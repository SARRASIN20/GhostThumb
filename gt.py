"""GhostThumb en ligne de commande : python gt.py <commande>   (python gt.py -h pour l'aide).

Chaque commande ouvre puis referme le canal scrcpy ; « screen » mémorise les éléments numérotés
pour que « tap N » vise la dernière capture.
"""

import argparse
import sys
import time
from pathlib import Path

from ghostthumb.adbtools import PhoneError
from ghostthumb.phone import KEY_NAMES, STATE_DIR, Phone


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gt", description="Piloter le téléphone Android (scrcpy + adb).")
    parser.add_argument("--adb", action="store_true", help="gestes par « adb shell input » au lieu du canal scrcpy")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="état du téléphone")
    p = sub.add_parser("screen", help="capture + éléments numérotés")
    p.add_argument("--out", default=str(STATE_DIR / "screen.png"), help="où enregistrer l'image")
    p.add_argument("--no-image", action="store_true")
    p.add_argument("--no-elements", action="store_true")
    p.add_argument("--wait", type=float, default=0)
    p = sub.add_parser("tap", help="toucher : tap N | tap X Y | tap --text TEXTE")
    p.add_argument("where", nargs="*", type=float)
    p.add_argument("--text")
    p.add_argument("--long", action="store_true", help="appui long")
    p = sub.add_parser("type", help="écrire du texte (tout alphabet)")
    p.add_argument("text")
    p.add_argument("--element", type=int, help="toucher ce champ d'abord")
    p.add_argument("--clear", action="store_true", help="vider le champ avant")
    p.add_argument("--submit", action="store_true", help="appuyer sur Entrée après")
    p = sub.add_parser("scroll", help="faire défiler (down = voir la suite)")
    p.add_argument("direction", choices=["down", "up", "left", "right"])
    p.add_argument("--amount", type=float, default=0.6)
    p.add_argument("--element", type=int)
    p = sub.add_parser("swipe", help="glisser X1 Y1 X2 Y2 (coordonnées de l'image)")
    p.add_argument("coords", nargs=4, type=float)
    p.add_argument("--ms", type=int, default=300)
    p = sub.add_parser("key", help="touche système")
    p.add_argument("name", choices=KEY_NAMES)
    p = sub.add_parser("open", help="ouvrir une app (nom affiché ou paquet)")
    p.add_argument("app")
    p = sub.add_parser("apps", help="lister les apps")
    p.add_argument("--filter", default="")
    p.add_argument("--system", action="store_true")
    p.add_argument("--refresh", action="store_true")
    sub.add_parser("mirror", help="ouvrir la fenêtre scrcpy sur le PC")
    sub.add_parser("wifi", help="passer en Wi-Fi (téléphone branché en USB)")
    return parser


def dispatch(phone: Phone, args):
    if args.cmd == "status":
        return phone.status()
    if args.cmd == "screen":
        res = phone.look(not args.no_image, not args.no_elements, args.wait)
        if res.png:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(res.png)
            res.text += f"\nImage : {out}"
        return res
    if args.cmd == "tap":
        if args.text:
            return phone.tap(text=args.text, long_press=args.long)
        if len(args.where) == 1:
            return phone.tap(element=int(args.where[0]), long_press=args.long)
        if len(args.where) == 2:
            return phone.tap(x=args.where[0], y=args.where[1], long_press=args.long)
        raise PhoneError("tap N, ou tap X Y, ou tap --text TEXTE")
    if args.cmd == "type":
        return phone.type_text(args.text, element=args.element, clear=args.clear, submit=args.submit)
    if args.cmd == "scroll":
        return phone.scroll(args.direction, args.amount, args.element)
    if args.cmd == "swipe":
        return phone.swipe(*args.coords, duration_ms=args.ms)
    if args.cmd == "key":
        return phone.key(args.name)
    if args.cmd == "open":
        return phone.open_app(args.app)
    if args.cmd == "apps":
        return phone.apps_text(args.filter, args.refresh, args.system)
    if args.cmd == "mirror":
        return phone.mirror("open")
    if args.cmd == "wifi":
        return phone.wifi()
    raise PhoneError(f"commande inconnue : {args.cmd}")


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    phone = Phone(use_scrcpy=not args.adb)
    phone.load_state()
    try:
        print(dispatch(phone, args).text)
        return 0
    except PhoneError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 1
    finally:
        if phone.hands is not None:
            time.sleep(0.2)  # laisser le serveur traiter les derniers messages avant de couper
        phone.close()


if __name__ == "__main__":
    sys.exit(main())
