"""Le téléphone vu par l'IA : regarder, toucher, écrire, faire défiler, ouvrir une app.

Les coordonnées x,y que l'IA donne sont celles de la DERNIÈRE image renvoyée ; elles sont
converties ici en pixels de l'écran. Les numéros d'éléments renvoient à la dernière capture.
"""

from __future__ import annotations

import json
import re
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from .adbtools import EXE, NO_WINDOW, Adb, PhoneError, choose_device, scrcpy_version
from .config import ROOT, load_config
from .control import KEYCODES, AdbHands, ScrcpyHands
from .image import fit, looks_black, parse_screencap, scale_for, to_png
from .uitree import Element, extract_elements, find_by_text, fold, format_elements, parse_hierarchy

STATE_DIR = ROOT / ".state"
JOURNAL_DIR = ROOT / "journal"

UI_DUMP = "/data/local/tmp/ghostthumb-ui.xml"
UI_DUMP_CMD = f"rm -f {UI_DUMP}; uiautomator dump {UI_DUMP} >/dev/null 2>&1; cat {UI_DUMP} 2>/dev/null; rm -f {UI_DUMP}"
FOCUS_CMD = "dumpsys window | grep -E 'mCurrentFocus=|mFocusedApp='"
STATE_CMD = (FOCUS_CMD + "; dumpsys input_method | grep -E -m1 'mInputShown='"
             "; dumpsys activity activities | grep -E -m1 'mKeyguardShowing='"
             "; dumpsys power | grep -E -m1 'mWakefulness='")

SPECIAL_KEYS = ("notifications", "quick_settings", "close_panels", "select_all")
KEY_NAMES = sorted(list(KEYCODES) + list(SPECIAL_KEYS))
# Touches permises même dans une app protégée : elles servent à en sortir ou ne touchent à rien dedans.
SAFE_KEYS = {"home", "back", "recents", "wakeup", "sleep", "power", "volume_up", "volume_down", "mute",
             "close_panels"}
SCRCPY_RETRY_SECONDS = 60

APP_LINE = re.compile(r"^ ([*-]) (.*?\S) {2,}(\S+)$")
APP_NAME_ONLY = re.compile(r"^ ([*-]) (.+)$")
APP_PACKAGE_ONLY = re.compile(r"^\s{20,}(\S+)$")
PACKAGE_RE = re.compile(r"[A-Za-z]\w*(\.[A-Za-z_]\w*)+")


@dataclass
class Result:
    text: str
    png: bytes | None = None


def parse_state(text: str) -> dict:
    state = {"package": "", "window": "", "keyboard": None, "locked": None, "awake": None}
    m = re.search(r"mFocusedApp=.*?\bu\d+\s+([\w.]+)/", text)
    if m:
        state["package"] = m.group(1)
    m = re.search(r"mCurrentFocus=Window\{\S+\s+u\d+\s+([^}\s]+)", text)
    if m:
        state["window"] = m.group(1)
        if not state["package"] and "/" in m.group(1):
            state["package"] = m.group(1).split("/", 1)[0]
    for key, pattern in (("keyboard", r"mInputShown=(true|false)"), ("locked", r"mKeyguardShowing=(true|false)")):
        m = re.search(pattern, text)
        if m:
            state[key] = m.group(1) == "true"
    m = re.search(r"mWakefulness=(\w+)", text)
    if m:
        state["awake"] = m.group(1) == "Awake"
    return state


def parse_app_list(text: str) -> list[dict]:
    """Sortie de « scrcpy-server list_apps=true » : ' - Nom<espaces> paquet' (« * » = app système)."""
    apps, pending = [], None
    for line in text.splitlines():
        line = line.rstrip("\r")
        m = APP_LINE.match(line)
        if m:
            apps.append({"label": m.group(2), "package": m.group(3), "system": m.group(1) == "*"})
            pending = None
            continue
        m = APP_PACKAGE_ONLY.match(line)
        if m and pending:
            apps.append({**pending, "package": m.group(1)})
            pending = None
            continue
        m = APP_NAME_ONLY.match(line)
        if m:
            pending = {"label": m.group(2).strip(), "system": m.group(1) == "*"}
    return apps


class Phone:
    def __init__(self, config: dict | None = None, use_scrcpy: bool = True):
        self.cfg = config or load_config()
        self.use_scrcpy = use_scrcpy
        self.lock = threading.RLock()
        self._adb: Adb | None = None
        self._version: str | None = None
        self.serial: str | None = None
        self.hands = None
        self.hands_note = ""
        self._scrcpy_failed_at = 0.0
        self.screen_size: tuple[int, int] | None = None
        self.scale: float | None = None
        self.elements: list[Element] = []
        self.last_activity = time.monotonic()
        self._apps: list[dict] | None = None
        self._viewer: subprocess.Popen | None = None

    # --- appareil, échelle, mains -------------------------------------------------------------

    @property
    def adb(self) -> Adb:
        if self._adb is None:
            self._adb = Adb()
        return self._adb

    @property
    def version(self) -> str:
        if self._version is None:
            self._version = scrcpy_version(self.adb.scrcpy_dir)
        return self._version

    def ensure_device(self) -> str:
        dev = choose_device(self.adb.devices(), self.cfg.get("serial", ""))
        if dev.serial != self.serial:
            self.release_hands()
            self.serial = dev.serial
            self.screen_size = None
            self.scale = None
            self.elements = []
            self._apps = None
        return self.serial

    def _screen(self) -> tuple[int, int]:
        if self.screen_size is None:
            head = self.adb.exec_out(self.serial, "screencap | head -c 12", timeout=20)
            if len(head) < 8:
                raise PhoneError("Taille de l'écran inconnue (capture impossible).")
            self.screen_size = struct.unpack_from("<II", head, 0)
        return self.screen_size

    def _scale(self) -> float:
        if self.scale is None:
            w, h = self._screen()
            self.scale = scale_for(w, h, self.cfg["image_max_side"])
        return self.scale

    def to_device(self, x: float, y: float) -> tuple[int, int]:
        scale = self._scale()
        w, h = self._screen()
        return min(w - 1, max(0, round(float(x) / scale))), min(h - 1, max(0, round(float(y) / scale)))

    def get_hands(self):
        self.ensure_device()
        if self.hands is not None and self.hands.alive():
            if isinstance(self.hands, ScrcpyHands):
                self.hands.screen_size = self._screen()
                return self.hands
            if not self.use_scrcpy or time.monotonic() - self._scrcpy_failed_at < SCRCPY_RETRY_SECONDS:
                return self.hands
        self.release_hands()
        if self.use_scrcpy:
            hands = ScrcpyHands(self.adb, self.serial, self.adb.scrcpy_dir / "scrcpy-server", self.version,
                                self._screen(), show_touches=self.cfg["show_touches"],
                                keep_active=self.cfg["keep_screen_on"])
            try:
                hands.start()
                self.hands, self.hands_note = hands, ""
                return hands
            except PhoneError as e:
                self._scrcpy_failed_at = time.monotonic()
                self.hands_note = f"canal scrcpy indisponible, repli sur adb input : {e}"
        self.hands = AdbHands(self.adb, self.serial)
        return self.hands

    def release_hands(self) -> None:
        if self.hands is not None:
            hands, self.hands = self.hands, None
            hands.close()

    def release_if_idle(self) -> None:
        if not self.lock.acquire(blocking=False):
            return
        try:
            idle = time.monotonic() - self.last_activity
            if isinstance(self.hands, ScrcpyHands) and idle > self.cfg["idle_release_seconds"]:
                self.release_hands()
        finally:
            self.lock.release()

    def close(self) -> None:
        with self.lock:
            self.release_hands()

    # --- état, garde-fou, journal --------------------------------------------------------------

    def state(self) -> dict:
        return parse_state(self.adb.shell(self.serial, STATE_CMD))

    def foreground(self) -> str:
        return parse_state(self.adb.shell(self.serial, FOCUS_CMD))["package"]

    def is_protected(self, package: str) -> bool:
        p = (package or "").lower()
        return bool(p) and (p in self.cfg["protected_packages"] or any(k in p for k in self.cfg["protected_keywords"]))

    def _begin(self, guard: bool = True):
        self.last_activity = time.monotonic()
        self.ensure_device()
        package = self.foreground()
        if guard and self.is_protected(package):
            self.journal("refus", raison="action dans une app protégée", app=package)
            raise PhoneError(
                f"Action bloquée : « {package} » est une app protégée (banque, paiement, argent). "
                "L'IA peut seulement en sortir (phone_key home ou back). Pour l'autoriser, l'utilisateur "
                "doit la retirer de GhostThumb\\config.json.")
        return self.get_hands(), package

    def journal(self, action: str, **detail) -> None:
        record = {"t": datetime.now().isoformat(timespec="seconds"), "action": action, "serial": self.serial, **detail}
        JOURNAL_DIR.mkdir(exist_ok=True)
        with (JOURNAL_DIR / f"{datetime.now():%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _after(self, message: str, look: bool) -> Result:
        if self.hands_note:
            message += f" ({self.hands_note})"
            self.hands_note = ""
        if not look:
            return Result(message)
        time.sleep(self.cfg["settle_seconds"])
        seen = self.look()
        return Result(message + "\n\n" + seen.text, seen.png)

    def save_state(self) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        data = {"serial": self.serial, "screen_size": self.screen_size, "scale": self.scale, "time": time.time(),
                "elements": [e.to_dict() for e in self.elements]}
        (STATE_DIR / "last_look.json").write_text(json.dumps(data, ensure_ascii=False), "utf-8")

    def load_state(self, max_age: float = 900) -> None:
        path = STATE_DIR / "last_look.json"
        if not path.is_file():
            return
        data = json.loads(path.read_text("utf-8"))
        if time.time() - data.get("time", 0) > max_age:
            return
        self.serial = data["serial"]
        self.screen_size = tuple(data["screen_size"]) if data.get("screen_size") else None
        self.scale = data.get("scale")
        self.elements = [Element.from_dict(d) for d in data.get("elements", [])]

    # --- regarder ------------------------------------------------------------------------------

    def look(self, image: bool = True, elements: bool = True, wait: float = 0.0) -> Result:
        with self.lock:
            self.last_activity = time.monotonic()
            if wait:
                time.sleep(min(max(float(wait), 0.0), 10.0))
            serial = self.ensure_device()
            out: dict = {}

            def job(name, fn):
                try:
                    out[name] = fn()
                except Exception as e:  # rapporté dans le texte, la capture continue
                    out[name] = e

            jobs = [("state", lambda: self.adb.shell(serial, STATE_CMD))]
            if image:
                jobs.append(("shot", lambda: self.adb.exec_out(serial, "screencap", timeout=40)))
            if elements:
                jobs.append(("ui", lambda: self.adb.exec_out(serial, UI_DUMP_CMD, timeout=40).decode("utf-8", "replace")))
            threads = [threading.Thread(target=job, args=j, daemon=True) for j in jobs]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            notes, png = [], None
            shot = out.get("shot")
            if isinstance(shot, bytes):
                try:
                    frame = parse_screencap(shot)
                    self.screen_size = (frame.width, frame.height)
                    self.scale = scale_for(frame.width, frame.height, self.cfg["image_max_side"])
                    png = to_png(frame, *fit(frame.width, frame.height, self.cfg["image_max_side"]),
                                 self.cfg["image_color_bits"])
                    if looks_black(frame):
                        notes.append("Capture noire : écran éteint, ou app qui interdit les captures "
                                     "(banque, saisie de mot de passe).")
                except PhoneError as e:
                    notes.append(str(e))
            elif isinstance(shot, Exception):
                notes.append(f"Capture impossible : {shot}")

            w, h = self._screen()
            scale = self._scale()
            ui = out.get("ui")
            self.elements = []
            if elements:
                if isinstance(ui, str) and "<hierarchy" in ui:
                    try:
                        nodes, _ = parse_hierarchy(ui)
                        self.elements = extract_elements(nodes, w, h, self.cfg["max_elements"])
                    except PhoneError as e:
                        notes.append(str(e))
                else:
                    notes.append("Arbre d'interface indisponible (animation ou vidéo en cours ?) : "
                                 "se fier à l'image, ou réessayer.")
            st = parse_state(out["state"]) if isinstance(out.get("state"), str) else {}

            header = f"Écran {w}×{h}"
            if png:
                iw, ih = fit(w, h, self.cfg["image_max_side"])
                header += f" · image {iw}×{ih} : les x,y se lisent sur cette image"
            app = st.get("package") or "?"
            facts = [f"app : {app}"]
            if st.get("awake") is False:
                facts.append("écran ÉTEINT (phone_key wakeup)")
            if st.get("locked"):
                facts.append("écran VERROUILLÉ")
            if st.get("keyboard"):
                facts.append("clavier affiché")
            if self.is_protected(app):
                facts.append("app PROTÉGÉE : actions bloquées")
            lines = [header, " · ".join(facts), *notes]
            if self.elements:
                lines.append(f"{len(self.elements)} éléments (numéros valables jusqu'à la prochaine capture) :")
                lines.append(format_elements(self.elements, scale))
            self.save_state()
            return Result("\n".join(lines), png)

    # --- agir ----------------------------------------------------------------------------------

    def element(self, number: int) -> Element:
        for el in self.elements:
            if el.number == number:
                return el
        if not self.elements:
            raise PhoneError("Aucun élément en mémoire : appelle d'abord phone_screen.")
        raise PhoneError(f"Pas d'élément n°{number} (la dernière capture en a {len(self.elements)}).")

    def _protected_labels(self) -> set[str]:
        """Noms affichés des apps protégées, d'après la liste d'apps déjà connue (sans la relire)."""
        apps = self._apps
        cache = self._apps_cache()
        if apps is None and cache.is_file():
            apps = json.loads(cache.read_text("utf-8"))
        return {fold(a["label"]) for a in apps or [] if a["label"] and self.is_protected(a["package"])}

    def _refuse_protected_icon(self, dx: int, dy: int, label: str | None) -> None:
        # Toucher l'icône d'une app protégée dans le lanceur reviendrait à l'ouvrir.
        if label is None:
            under = [e for e in self.elements
                     if e.bounds[0] <= dx < e.bounds[2] and e.bounds[1] <= dy < e.bounds[3]]
            label = min(under, key=lambda e: (e.bounds[2] - e.bounds[0]) * (e.bounds[3] - e.bounds[1])).label \
                if under else ""
        if label and fold(label) in self._protected_labels():
            self.journal("refus", raison="icône d'une app protégée", cible=label)
            raise PhoneError(f"Appui bloqué : « {label} » ouvre une app protégée (banque, paiement). "
                             "L'utilisateur l'ouvre lui-même.")

    def _target(self, element=None, x=None, y=None, text=None) -> tuple[int, int, str]:
        if element is not None:
            el = self.element(int(element))
            cx, cy = el.center
            self._refuse_protected_icon(cx, cy, el.label)
            return cx, cy, f"[{el.number}] {el.kind} « {el.label} »"
        if text:
            ui = self.adb.exec_out(self.serial, UI_DUMP_CMD, timeout=40).decode("utf-8", "replace")
            if "<hierarchy" not in ui:
                raise PhoneError("Interface illisible pour chercher ce texte : vise un numéro ou des coordonnées.")
            w, h = self._screen()
            # Liste locale : la numérotation que l'IA a vue reste celle de sa dernière capture.
            current = extract_elements(parse_hierarchy(ui)[0], w, h, self.cfg["max_elements"])
            hits = find_by_text(current, text)
            if not hits:
                raise PhoneError(f"Aucun élément visible ne correspond à « {text} ».")
            el = hits[0]
            more = f" ({len(hits)} correspondances, la première prise)" if len(hits) > 1 else ""
            cx, cy = el.center
            self._refuse_protected_icon(cx, cy, el.label)
            return cx, cy, f"{el.kind} « {el.label} »{more}"
        if x is not None and y is not None:
            dx, dy = self.to_device(x, y)
            self._refuse_protected_icon(dx, dy, None)
            return dx, dy, f"point ({x},{y}) de l'image"
        raise PhoneError("Indique element, ou x et y, ou text.")

    def tap(self, element=None, x=None, y=None, text=None, long_press=False, look=False) -> Result:
        with self.lock:
            hands, package = self._begin()
            dx, dy, what = self._target(element, x, y, text)
            (hands.long_press if long_press else hands.tap)(dx, dy)
            verb = "Appui long" if long_press else "Appui"
            self.journal(verb.lower(), cible=what, x=dx, y=dy, app=package)
            return self._after(f"{verb} sur {what}.", look)

    def type_text(self, text: str, element=None, x=None, y=None, clear=False, submit=False, look=False) -> Result:
        with self.lock:
            hands, package = self._begin()
            what = ""
            if element is not None or (x is not None and y is not None):
                dx, dy, what = self._target(element, x, y)
                hands.tap(dx, dy)
                time.sleep(0.5)
            if clear:
                hands.clear_field()
                time.sleep(0.15)
            how = hands.type_text(text, self.cfg["restore_clipboard"]) if text else "rien à écrire"
            if submit:
                time.sleep(0.1)
                hands.key(KEYCODES["enter"])
            self.journal("saisie", caracteres=len(text), methode=how, champ=what, efface=clear, valide=submit,
                         app=package)
            parts = [f"{len(text)} caractères saisis ({how})"]
            if what:
                parts.insert(0, f"dans {what}")
            if clear:
                parts.append("champ vidé avant")
            if submit:
                parts.append("Entrée pressée")
            return self._after(", ".join(parts) + ".", look)

    def scroll(self, direction: str, amount: float = 0.6, element=None, look=False) -> Result:
        direction = (direction or "").lower().strip()
        if direction not in ("up", "down", "left", "right"):
            raise PhoneError("direction : up, down, left ou right (sens où l'on veut aller dans le contenu).")
        with self.lock:
            hands, package = self._begin()
            w, h = self._screen()
            if element is not None:
                x1, y1, x2, y2 = self.element(int(element)).bounds
            else:
                x1, y1, x2, y2 = 0, int(h * 0.2), w, int(h * 0.8)
            amount = min(max(float(amount), 0.1), 0.9)
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            hx, hy = int((x2 - x1) * amount / 2), int((y2 - y1) * amount / 2)
            start, end = {
                "down": ((cx, cy + hy), (cx, cy - hy)),
                "up": ((cx, cy - hy), (cx, cy + hy)),
                "right": ((cx + hx, cy), (cx - hx, cy)),
                "left": ((cx - hx, cy), (cx + hx, cy)),
            }[direction]

            def inside(p):  # loin des bords : ils déclenchent les gestes système retour/accueil
                return (min(max(p[0], int(w * 0.08)), int(w * 0.92)), min(max(p[1], int(h * 0.08)), int(h * 0.92)))

            (sx, sy), (ex, ey) = inside(start), inside(end)
            hands.swipe(sx, sy, ex, ey, duration_ms=450, hold_ms=250)
            self.journal("défilement", sens=direction, quantite=amount, app=package)
            return self._after(f"Défilement vers {direction} ({int(amount * 100)} % de la zone).", look)

    def swipe(self, x1, y1, x2, y2, duration_ms=300, look=False) -> Result:
        with self.lock:
            hands, package = self._begin()
            a, b = self.to_device(x1, y1), self.to_device(x2, y2)
            duration_ms = int(min(max(float(duration_ms), 50), 5000))
            hands.swipe(*a, *b, duration_ms=duration_ms)
            self.journal("glissement", de=a, a=b, ms=duration_ms, app=package)
            return self._after(f"Glissement de ({x1},{y1}) à ({x2},{y2}) en {duration_ms} ms.", look)

    def key(self, name: str, look=False) -> Result:
        name = (name or "").lower().strip()
        if name not in KEYCODES and name not in SPECIAL_KEYS:
            raise PhoneError(f"Touche inconnue « {name} ». Possibles : {', '.join(KEY_NAMES)}.")
        with self.lock:
            hands, package = self._begin(guard=name not in SAFE_KEYS)
            if name == "notifications":
                hands.expand_notifications()
            elif name == "quick_settings":
                hands.expand_quick_settings()
            elif name == "close_panels":
                hands.collapse_panels()
            elif name == "select_all":
                hands.select_all()
            else:
                hands.key(KEYCODES[name])
            self.journal("touche", touche=name, app=package)
            return self._after(f"Touche « {name} » envoyée.", look)

    # --- applications ----------------------------------------------------------------------------

    def _apps_cache(self):
        return STATE_DIR / f"apps-{re.sub(r'[^A-Za-z0-9]', '_', self.serial or '')}.json"

    def apps(self, refresh: bool = False) -> list[dict]:
        serial = self.ensure_device()
        if self._apps is not None and not refresh:
            return self._apps
        cache = self._apps_cache()
        if not refresh and cache.is_file() and time.time() - cache.stat().st_mtime < 86400:
            self._apps = json.loads(cache.read_text("utf-8"))
            return self._apps
        remote = "/data/local/tmp/ghostthumb-list.jar"
        self.adb.run(["push", str(self.adb.scrcpy_dir / "scrcpy-server"), remote], serial=serial, timeout=60)
        out = self.adb.shell(serial, f"CLASSPATH={remote} app_process / com.genymobile.scrcpy.Server "
                                     f"{self.version} log_level=info list_apps=true", timeout=180)
        apps = parse_app_list(out)
        if not apps:  # repli : paquets seuls, sans nom affiché
            listing = self.adb.shell(serial, "pm list packages -3 --user 0")
            apps = [{"label": "", "package": line.split(":", 1)[1].strip(), "system": False}
                    for line in listing.splitlines() if line.startswith("package:")]
        STATE_DIR.mkdir(exist_ok=True)
        cache.write_text(json.dumps(apps, ensure_ascii=False), "utf-8")
        self._apps = apps
        return apps

    def resolve_app(self, query: str) -> tuple[str, str]:
        q = (query or "").strip()
        if not q:
            raise PhoneError("Nom d'application vide.")
        apps = self.apps()
        for a in apps:
            if a["package"] == q:
                return q, a["label"] or q
        fq = fold(q)
        for test in (lambda s: s == fq, lambda s: s.startswith(fq), lambda s: fq in s):
            hits = [a for a in apps if a["label"] and test(fold(a["label"]))]
            if len(hits) > 1:
                users = [a for a in hits if not a["system"]]
                hits = users if len(users) == 1 else hits
            if len(hits) == 1:
                return hits[0]["package"], hits[0]["label"]
            if hits:
                choices = ", ".join(f"{a['label']} ({a['package']})" for a in hits[:8])
                raise PhoneError(f"Plusieurs apps correspondent à « {q} » : {choices}. Précise le paquet.")
        hits = [a for a in apps if fq in a["package"].lower()]
        if len(hits) == 1:
            return hits[0]["package"], hits[0]["label"] or hits[0]["package"]
        if PACKAGE_RE.fullmatch(q):
            return q, q
        raise PhoneError(f"Aucune app « {q} » trouvée. phone_apps donne la liste.")

    def open_app(self, app: str, look=False) -> Result:
        with self.lock:
            self.last_activity = time.monotonic()
            self.ensure_device()
            package, label = self.resolve_app(app)
            if self.is_protected(package):
                self.journal("refus", raison="ouverture d'une app protégée", app=package)
                raise PhoneError(f"Ouverture bloquée : {label} ({package}) est une app protégée (banque, paiement). "
                                 "L'utilisateur l'ouvre lui-même, ou la retire de GhostThumb\\config.json.")
            hands = self.get_hands()
            hands.start_app(package)
            time.sleep(1.2)
            front = self.foreground()
            self.journal("ouverture", app=package)
            if front == package:
                message = f"{label} ({package}) est ouverte."
            else:
                message = f"Lancement de {label} ({package}) demandé ; au premier plan : {front or '?'} (chargement en cours ?)."
            return self._after(message, look)

    def apps_text(self, filter: str = "", refresh: bool = False, include_system: bool = False) -> Result:
        with self.lock:
            self.last_activity = time.monotonic()
            apps = self.apps(refresh)
            chosen = [a for a in apps if include_system or not a["system"]]
            if filter:
                f = fold(filter)
                chosen = [a for a in chosen if f in fold(a["label"]) or f in a["package"].lower()]
            lines = [f"{len(chosen)} apps" + (" (système comprises)" if include_system else " installées par l'utilisateur") + " :"]
            for a in sorted(chosen, key=lambda a: fold(a["label"] or a["package"])):
                mark = " [protégée]" if self.is_protected(a["package"]) else ""
                lines.append(f"- {a['label'] or '?'} : {a['package']}{mark}")
            return Result("\n".join(lines))

    # --- état général, recopie, Wi-Fi ----------------------------------------------------------

    def status(self) -> Result:
        with self.lock:
            devices = self.adb.devices()
            try:
                serial = self.ensure_device()
            except PhoneError as e:
                seen = ", ".join(f"{d.serial} ({d.state})" for d in devices)
                return Result(str(e) + (f"\nVus par adb : {seen}" if seen else ""))
            sections = self.adb.shell(serial, "echo @; getprop ro.product.manufacturer; getprop ro.product.model; "
                                              "getprop ro.build.version.release; echo @; dumpsys battery").split("@")
            props = [line.strip() for line in sections[1].strip().splitlines()] if len(sections) > 1 else []
            battery = sections[2] if len(sections) > 2 else ""
            st = self.state()
            self.screen_size = self.scale = None
            w, h = self._screen()
            dev = next((d for d in devices if d.serial == serial), None)
            maker, model, release = (props + ["?", "?", "?"])[:3]
            lines = [f"Téléphone : {maker} {model} (Android {release}) · {'Wi-Fi' if dev and dev.is_tcp else 'USB'} · série {serial}"]
            screen = "allumé" if st.get("awake") else ("éteint" if st.get("awake") is False else "?")
            locked = {True: "oui", False: "non", None: "?"}[st.get("locked")]
            lines.append(f"Écran : {w}×{h} · {screen} · verrouillé : {locked}")
            level = re.search(r"level:\s*(\d+)", battery)
            plugged = [src for src, key in (("secteur", "AC powered"), ("USB", "USB powered"))
                       if re.search(rf"{key}:\s*true", battery)]
            if level:
                lines.append(f"Batterie : {level.group(1)} %" + (f" · branchée ({', '.join(plugged)})" if plugged else ""))
            app = st.get("package") or "?"
            lines.append(f"Au premier plan : {app}" + (" [protégée]" if self.is_protected(app) else ""))
            if isinstance(self.hands, ScrcpyHands) and self.hands.alive():
                hands = f"canal scrcpy {self.version} ouvert"
            elif isinstance(self.hands, AdbHands):
                hands = "repli adb input"
            else:
                hands = f"canal scrcpy {self.version} (s'ouvre à la première action)"
            lines.append(f"Mains : {hands}")
            others = [f"{d.serial} ({d.state})" for d in devices if d.serial != serial]
            if others:
                lines.append("Autres appareils : " + ", ".join(others))
            return Result("\n".join(lines))

    def mirror(self, action: str = "open") -> Result:
        with self.lock:
            running = self._viewer is not None and self._viewer.poll() is None
            if action == "close":
                if not running:
                    return Result("Aucune fenêtre de recopie ouverte par GhostThumb.")
                self._viewer.terminate()
                self._viewer = None
                return Result("Fenêtre de recopie fermée.")
            serial = self.ensure_device()
            if running:
                return Result("La fenêtre de recopie est déjà ouverte sur le PC.")
            exe = self.adb.scrcpy_dir / f"scrcpy{EXE}"
            self._viewer = subprocess.Popen(
                [str(exe), "-s", serial, "--window-title", "Telephone - GhostThumb", "--no-audio", "--stay-awake"],
                cwd=str(self.adb.scrcpy_dir), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            time.sleep(2.5)
            if self._viewer.poll() is not None:
                code, self._viewer = self._viewer.returncode, None
                raise PhoneError(f"scrcpy s'est fermé aussitôt (code {code}).")
            self.journal("recopie", etat="ouverte")
            return Result("Fenêtre scrcpy ouverte sur le PC : le téléphone s'y voit en direct, "
                          "et la souris permet de reprendre la main à tout moment.")

    def wifi(self) -> Result:
        with self.lock:
            serial = self.ensure_device()
            if ":" in serial:
                return Result(f"Déjà connecté en Wi-Fi ({serial}).")
            out = self.adb.shell(serial, "ip -f inet addr show wlan0")
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", out)
            if not m:
                raise PhoneError("Pas d'adresse Wi-Fi : active le Wi-Fi du téléphone, sur le même réseau que le PC.")
            ip = m.group(1)
            self.release_hands()
            self.adb.run(["tcpip", "5555"], serial=serial, timeout=20)
            time.sleep(2.5)
            out = self.adb.run(["connect", f"{ip}:5555"], timeout=20, check=False)
            if "connected" not in out:
                raise PhoneError(f"Connexion Wi-Fi échouée : {out.strip()}")
            self.serial = None
            self.journal("wifi", adresse=f"{ip}:5555")
            return Result(f"Connecté en Wi-Fi ({ip}:5555). Le câble peut être débranché ; valable jusqu'au "
                          "prochain redémarrage du téléphone (ensuite : rebrancher et relancer).")
