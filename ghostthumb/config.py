"""Réglages : valeurs par défaut, surchargées par config.json puis par l'environnement."""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

DEFAULTS = {
    # Numéro de série adb à utiliser si plusieurs téléphones sont branchés ("" = automatique, USB d'abord).
    "serial": "",
    # Côté le plus long de la capture envoyée à l'IA (pixels). Plus petit = moins de jetons, moins lisible.
    "image_max_side": 1280,
    # Bits gardés par couleur : 5 rend la capture ~3 fois plus légère, le texte reste net.
    "image_color_bits": 5,
    "max_elements": 90,
    # Points blancs à l'écran du téléphone là où l'IA touche (rétabli à la fin).
    "show_touches": True,
    # Empêche l'écran de s'éteindre pendant que l'IA pilote.
    "keep_screen_on": True,
    # Ferme le canal scrcpy après ce délai d'inactivité (l'écran peut alors se mettre en veille).
    "idle_release_seconds": 180,
    # Après un collage, remet dans le presse-papiers ce qui s'y trouvait avant.
    "restore_clipboard": True,
    # Attente après une action quand look=true, pour laisser l'écran se stabiliser.
    "settle_seconds": 0.8,
    # Apps où l'IA ne peut ni toucher ni écrire (elle peut seulement en sortir avec home/back).
    # Un mot-clé protège tout paquet qui le contient ; ajouter ici les paquets qu'aucun mot-clé n'attrape.
    "protected_packages": [],
    "protected_keywords": ["bank", "banque", "wallet", "pay", "money", "finance", "crypto", "binance"],
}


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = dict(DEFAULTS)
    if path.is_file():
        with path.open(encoding="utf-8") as f:
            user = json.load(f)
        cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
    serial = os.environ.get("GHOSTTHUMB_SERIAL")
    if serial:
        cfg["serial"] = serial
    cfg["protected_packages"] = [p.lower() for p in cfg["protected_packages"]]
    cfg["protected_keywords"] = [k.lower() for k in cfg["protected_keywords"]]
    return cfg
