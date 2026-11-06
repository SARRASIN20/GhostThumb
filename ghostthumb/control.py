"""Les mains de l'IA.

ScrcpyHands parle directement au serveur de scrcpy (lancé sans vidéo ni audio, contrôle seul) :
gestes instantanés et saisie de n'importe quel texte (accents, arabe, émojis) par le presse-papiers.
AdbHands est le repli « adb shell input » : plus lent, texte ASCII seulement.

Format des messages : scrcpy 5.0.1, server/.../control/ControlMessageReader.java (gros-boutiste).
Sans vidéo, le serveur applique les coordonnées telles quelles (Controller.getEventPointAndDisplayId).
"""

from __future__ import annotations

import itertools
import random
import socket
import struct
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from .adbtools import NO_WINDOW, Adb, PhoneError

TYPE_INJECT_KEYCODE = 0
TYPE_INJECT_TEXT = 1
TYPE_INJECT_TOUCH_EVENT = 2
TYPE_INJECT_SCROLL_EVENT = 3
TYPE_EXPAND_NOTIFICATION_PANEL = 5
TYPE_EXPAND_SETTINGS_PANEL = 6
TYPE_COLLAPSE_PANELS = 7
TYPE_GET_CLIPBOARD = 8
TYPE_SET_CLIPBOARD = 9
TYPE_START_APP = 16

DEVICE_MSG_CLIPBOARD = 0
DEVICE_MSG_ACK_CLIPBOARD = 1
DEVICE_MSG_UHID_OUTPUT = 2

ACTION_DOWN, ACTION_UP, ACTION_MOVE = 0, 1, 2
POINTER_ID_GENERIC_FINGER = -2
INJECT_TEXT_MAX_LENGTH = 300
CLIPBOARD_TEXT_MAX_BYTES = (1 << 18) - 14

KEYCODES = {
    "home": 3, "back": 4, "call": 5, "endcall": 6,
    "dpad_up": 19, "dpad_down": 20, "dpad_left": 21, "dpad_right": 22, "dpad_center": 23,
    "volume_up": 24, "volume_down": 25, "power": 26, "camera": 27,
    "tab": 61, "space": 62, "enter": 66, "delete": 67, "menu": 82, "search": 84,
    "play_pause": 85, "page_up": 92, "page_down": 93, "escape": 111, "forward_delete": 112,
    "move_home": 122, "move_end": 123, "mute": 164, "recents": 187, "sleep": 223, "wakeup": 224,
    "cut": 277, "copy": 278, "paste": 279,
}
KEYCODE_A = 29
KEYCODE_CTRL_LEFT = 113
META_CTRL = 0x1000 | 0x2000  # META_CTRL_ON | META_CTRL_LEFT_ON


def u16_fixed(value: float) -> int:
    value = min(max(value, 0.0), 1.0)
    return 0xFFFF if value >= 1.0 else int(value * 0x10000)


def i16_fixed(value: float) -> int:
    value = min(max(value, -1.0), 1.0)
    return 0x7FFF if value >= 1.0 else int(value * 0x8000)


def msg_keycode(action: int, keycode: int, repeat: int = 0, meta: int = 0) -> bytes:
    return struct.pack(">BBiii", TYPE_INJECT_KEYCODE, action, keycode, repeat, meta)


def msg_text(text: str) -> bytes:
    data = text.encode("utf-8")
    return struct.pack(">BI", TYPE_INJECT_TEXT, len(data)) + data


def msg_touch(action: int, x: int, y: int, width: int, height: int, pressure: float = 1.0,
              pointer_id: int = POINTER_ID_GENERIC_FINGER) -> bytes:
    return struct.pack(">BBqiiHHHii", TYPE_INJECT_TOUCH_EVENT, action, pointer_id, x, y, width, height,
                       u16_fixed(pressure), 0, 0)


def msg_scroll(x: int, y: int, width: int, height: int, hscroll: float, vscroll: float) -> bytes:
    # Le serveur multiplie par 16 la valeur décodée : la plage utile est [-16, 16].
    return struct.pack(">BiiHHhhi", TYPE_INJECT_SCROLL_EVENT, x, y, width, height,
                       i16_fixed(hscroll / 16), i16_fixed(vscroll / 16), 0)


def msg_get_clipboard() -> bytes:
    return struct.pack(">BB", TYPE_GET_CLIPBOARD, 0)


def msg_set_clipboard(sequence: int, text: str, paste: bool) -> bytes:
    data = text.encode("utf-8")
    if len(data) > CLIPBOARD_TEXT_MAX_BYTES:
        raise PhoneError("Texte trop long pour le presse-papiers du téléphone.")
    return struct.pack(">BqBI", TYPE_SET_CLIPBOARD, sequence, 1 if paste else 0, len(data)) + data


def msg_start_app(name: str) -> bytes:
    data = name.encode("utf-8")
    if not data or len(data) > 255:
        raise PhoneError("Nom d'application vide ou trop long.")
    return struct.pack(">BB", TYPE_START_APP, len(data)) + data


def keyable(text: str) -> bool:
    """Texte que l'injection de touches sait taper (ASCII imprimable, sans retour à la ligne)."""
    return all(0x20 <= ord(c) <= 0x7E for c in text)


def swipe_points(x1: int, y1: int, x2: int, y2: int, steps: int) -> list[tuple[int, int]]:
    return [(round(x1 + (x2 - x1) * i / steps), round(y1 + (y2 - y1) * i / steps)) for i in range(1, steps + 1)]


class ControlChannel:
    """Socket de contrôle déjà établie : envoi des messages, lecture des messages de l'appareil."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.closed = False
        self._send_lock = threading.Lock()
        self._cond = threading.Condition()
        self._acks: set[int] = set()
        self._clipboard: str | None = None
        self._clipboard_count = 0
        self._reader = threading.Thread(target=self._read_loop, name="scrcpy-device-msg", daemon=True)
        self._reader.start()

    def send(self, data: bytes) -> None:
        if self.closed:
            raise PhoneError("Le canal scrcpy est fermé.")
        try:
            with self._send_lock:
                self.sock.sendall(data)
        except OSError as e:
            self.closed = True
            raise PhoneError(f"Canal scrcpy coupé ({e}).") from None

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return bytes(buf)

    def _read_loop(self) -> None:
        try:
            while True:
                kind = self._recv_exact(1)[0]
                if kind == DEVICE_MSG_CLIPBOARD:
                    (n,) = struct.unpack(">I", self._recv_exact(4))
                    text = self._recv_exact(n).decode("utf-8", "replace")
                    with self._cond:
                        self._clipboard = text
                        self._clipboard_count += 1
                        self._cond.notify_all()
                elif kind == DEVICE_MSG_ACK_CLIPBOARD:
                    (seq,) = struct.unpack(">q", self._recv_exact(8))
                    with self._cond:
                        self._acks.add(seq)
                        self._cond.notify_all()
                elif kind == DEVICE_MSG_UHID_OUTPUT:
                    _id, n = struct.unpack(">HH", self._recv_exact(4))
                    self._recv_exact(n)
                else:
                    break
        except (OSError, EOFError):
            pass
        finally:
            with self._cond:
                self.closed = True
                self._cond.notify_all()

    def wait_ack(self, sequence: int, timeout: float) -> bool:
        with self._cond:
            self._cond.wait_for(lambda: sequence in self._acks or self.closed, timeout)
            return sequence in self._acks

    def get_clipboard(self, timeout: float = 1.0) -> str | None:
        """Texte du presse-papiers, ou None s'il est vide (le serveur ne répond alors rien)."""
        with self._cond:
            before = self._clipboard_count
        self.send(msg_get_clipboard())
        with self._cond:
            self._cond.wait_for(lambda: self._clipboard_count != before or self.closed, timeout)
            return self._clipboard if self._clipboard_count != before else None

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def connect_control(port: int, timeout: float, server_alive=lambda: True, describe=lambda: "") -> socket.socket:
    """Se connecter au tunnel adb et attendre l'octet témoin que le serveur envoie à l'acceptation.

    Tant que le serveur n'écoute pas encore, adb accepte puis referme aussitôt : on recommence.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not server_alive():
            raise PhoneError("Le serveur scrcpy s'est arrêté au démarrage. " + describe())
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        except OSError:
            time.sleep(0.1)
            continue
        try:
            # Attente longue : refermer une connexion déjà acceptée par le serveur la perdrait pour de bon.
            sock.settimeout(max(0.5, deadline - time.monotonic()))
            first = sock.recv(1)
        except OSError:
            first = b""
        if first == b"\x00":
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return sock
        sock.close()
        time.sleep(0.1)
    raise PhoneError("Connexion au serveur scrcpy impossible (délai dépassé). " + describe())


class ScrcpyHands:
    name = "scrcpy"

    def __init__(self, adb: Adb, serial: str, server_file: Path, version: str, screen_size: tuple[int, int],
                 show_touches: bool = True, keep_active: bool = True):
        self.adb = adb
        self.serial = serial
        self.server_file = server_file
        self.version = version
        self.screen_size = screen_size
        self.show_touches = show_touches
        self.keep_active = keep_active
        self.channel: ControlChannel | None = None
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.logs: deque[str] = deque(maxlen=200)
        self._seq = itertools.count(1)

    # --- cycle de vie -------------------------------------------------------------------------

    def start(self, timeout: float = 15.0) -> None:
        scid = f"{random.randrange(0, 0x7FFFFFFF):08x}"
        remote_jar = f"/data/local/tmp/ghostthumb-{scid}.jar"
        self.adb.run(["push", str(self.server_file), remote_jar], serial=self.serial, timeout=60)
        out = self.adb.run(["forward", "tcp:0", f"localabstract:scrcpy_{scid}"], serial=self.serial)
        try:
            self.port = int(out.strip())
        except ValueError:
            raise PhoneError(f"adb forward a répondu « {out.strip()[:80]} ».") from None
        flag = lambda b: "true" if b else "false"  # noqa: E731
        cmd = [
            self.adb.path, "-s", self.serial, "shell", f"CLASSPATH={remote_jar}", "app_process", "/",
            "com.genymobile.scrcpy.Server", self.version, f"scid={scid}", "log_level=info",
            "video=false", "audio=false", "control=true", "tunnel_forward=true",
            "send_device_meta=false", "send_dummy_byte=true", "clipboard_autosync=false",
            "cleanup=true", "power_on=true",
            f"show_touches={flag(self.show_touches)}", f"keep_active={flag(self.keep_active)}",
        ]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        threading.Thread(target=self._pump_logs, name="scrcpy-server-log", daemon=True).start()
        try:
            sock = connect_control(self.port, timeout, lambda: self.proc.poll() is None, self.describe)
        except PhoneError:
            self.close()
            self.adb.shell(self.serial, f"rm -f {remote_jar}", timeout=10)
            raise
        self.channel = ControlChannel(sock)

    def _pump_logs(self) -> None:
        for raw in iter(self.proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.logs.append(line)

    def describe(self) -> str:
        tail = " | ".join(list(self.logs)[-4:])
        return f"Journal du serveur : {tail}" if tail else ""

    def alive(self) -> bool:
        return (self.channel is not None and not self.channel.closed
                and self.proc is not None and self.proc.poll() is None)

    def close(self) -> None:
        # Fermer la socket arrête le serveur ; son processus de nettoyage rétablit alors
        # « show_touches » et supprime le .jar poussé.
        if self.channel:
            self.channel.close()
            self.channel = None
        if self.proc:
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        if self.port:
            self.adb.run(["forward", "--remove", f"tcp:{self.port}"], serial=self.serial, check=False, timeout=10)
            self.port = None

    # --- gestes --------------------------------------------------------------------------------

    def _touch(self, action: int, x: int, y: int) -> None:
        w, h = self.screen_size
        self.channel.send(msg_touch(action, x, y, w, h, 0.0 if action == ACTION_UP else 1.0))

    def tap(self, x: int, y: int, hold_ms: int = 60) -> None:
        self._touch(ACTION_DOWN, x, y)
        time.sleep(hold_ms / 1000)
        self._touch(ACTION_UP, x, y)

    def long_press(self, x: int, y: int, hold_ms: int = 900) -> None:
        self.tap(x, y, hold_ms)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300, hold_ms: int = 0) -> None:
        steps = max(8, duration_ms // 16)
        self._touch(ACTION_DOWN, x1, y1)
        for x, y in swipe_points(x1, y1, x2, y2, steps):
            time.sleep(duration_ms / 1000 / steps)
            self._touch(ACTION_MOVE, x, y)
        if hold_ms:
            # Rester posé à l'arrivée annule l'élan : le contenu bouge d'autant, ni plus ni moins.
            time.sleep(hold_ms / 1000)
        self._touch(ACTION_UP, x2, y2)

    def key(self, keycode: int, meta: int = 0) -> None:
        self.channel.send(msg_keycode(ACTION_DOWN, keycode, 0, meta))
        self.channel.send(msg_keycode(ACTION_UP, keycode, 0, meta))

    def select_all(self) -> None:
        self.channel.send(msg_keycode(ACTION_DOWN, KEYCODE_CTRL_LEFT, 0, META_CTRL))
        self.key(KEYCODE_A, META_CTRL)
        self.channel.send(msg_keycode(ACTION_UP, KEYCODE_CTRL_LEFT, 0, 0))

    def clear_field(self) -> None:
        self.select_all()
        time.sleep(0.05)
        self.key(KEYCODES["delete"])

    def type_text(self, text: str, restore_clipboard: bool = True) -> str:
        if keyable(text):
            for i in range(0, len(text), INJECT_TEXT_MAX_LENGTH):
                self.channel.send(msg_text(text[i:i + INJECT_TEXT_MAX_LENGTH]))
            return "frappe"
        self.paste_text(text, restore_clipboard)
        return "collage"

    def paste_text(self, text: str, restore_clipboard: bool = True) -> None:
        previous = self.channel.get_clipboard(timeout=0.6) if restore_clipboard else None
        seq = next(self._seq)
        self.channel.send(msg_set_clipboard(seq, text, paste=True))
        if not self.channel.wait_ack(seq, timeout=3):
            raise PhoneError("Le téléphone n'a pas confirmé le presse-papiers. " + self.describe())
        if previous is not None and previous != text:
            time.sleep(0.6)  # laisser l'app traiter le collage avant de remettre l'ancien contenu
            seq = next(self._seq)
            self.channel.send(msg_set_clipboard(seq, previous, paste=False))
            self.channel.wait_ack(seq, timeout=2)

    def get_clipboard(self) -> str | None:
        return self.channel.get_clipboard()

    def start_app(self, package: str) -> None:
        mark = len(self.logs)
        self.channel.send(msg_start_app(package))
        time.sleep(0.8)
        for line in list(self.logs)[mark:]:
            if "No app found" in line or "No unique app" in line:
                raise PhoneError(f"Application « {package} » introuvable sur le téléphone.")

    def expand_notifications(self) -> None:
        self.channel.send(bytes([TYPE_EXPAND_NOTIFICATION_PANEL]))

    def expand_quick_settings(self) -> None:
        self.channel.send(bytes([TYPE_EXPAND_SETTINGS_PANEL]))

    def collapse_panels(self) -> None:
        self.channel.send(bytes([TYPE_COLLAPSE_PANELS]))


def _sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


class AdbHands:
    """Repli sans scrcpy : « adb shell input » (environ une demi-seconde par geste, ASCII seulement)."""

    name = "adb"

    def __init__(self, adb: Adb, serial: str):
        self.adb = adb
        self.serial = serial

    def _sh(self, command: str) -> str:
        return self.adb.shell(self.serial, command, timeout=20)

    def alive(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def tap(self, x: int, y: int, hold_ms: int = 60) -> None:
        self._sh(f"input tap {x} {y}")

    def long_press(self, x: int, y: int, hold_ms: int = 900) -> None:
        self._sh(f"input swipe {x} {y} {x} {y} {hold_ms}")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300, hold_ms: int = 0) -> None:
        self._sh(f"input swipe {x1} {y1} {x2} {y2} {duration_ms + hold_ms}")

    def key(self, keycode: int, meta: int = 0) -> None:
        self._sh(f"input keyevent {keycode}")

    def select_all(self) -> None:
        self._sh(f"input keycombination {KEYCODE_CTRL_LEFT} {KEYCODE_A}")

    def clear_field(self) -> None:
        self._sh(f"input keyevent {KEYCODES['move_end']} " + f"{KEYCODES['delete']} " * 120)

    def type_text(self, text: str, restore_clipboard: bool = True) -> str:
        if not keyable(text):
            raise PhoneError("Texte avec accents/caractères spéciaux : il faut le canal scrcpy, indisponible ici.")
        self._sh("input text " + _sh_quote(text.replace(" ", "%s")))
        return "frappe"

    def get_clipboard(self) -> str | None:
        raise PhoneError("Lecture du presse-papiers impossible sans le canal scrcpy.")

    def start_app(self, package: str) -> None:
        out = self._sh(f"monkey -p {_sh_quote(package)} -c android.intent.category.LAUNCHER 1")
        if "No activities found" in out or "Error" in out:
            raise PhoneError(f"Application « {package} » introuvable ou sans écran de lancement.")

    def expand_notifications(self) -> None:
        self._sh("cmd statusbar expand-notifications")

    def expand_quick_settings(self) -> None:
        self._sh("cmd statusbar expand-settings")

    def collapse_panels(self) -> None:
        self._sh("cmd statusbar collapse")
