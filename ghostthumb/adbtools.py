"""Trouver adb et scrcpy, lister les téléphones, exécuter des commandes adb."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import ROOT

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # évite les fenêtres console qui clignotent sous Windows
DEFAULT_SCRCPY_VERSION = "5.0.1"
EXE = ".exe" if os.name == "nt" else ""


class PhoneError(Exception):
    """Erreur lisible, renvoyée telle quelle à l'IA ou à l'utilisateur."""


NO_DEVICE_MSG = (
    "Aucun téléphone connecté. Branche-le en USB (débogage USB activé dans les Options pour les "
    "développeurs), ou reconnecte-le en Wi-Fi avec « python gt.py wifi » pendant qu'il est branché."
)
UNAUTHORIZED_MSG = (
    "Le téléphone refuse ce PC : sur son écran, accepte « Autoriser le débogage USB ? » en cochant "
    "« Toujours autoriser sur cet ordinateur ». C'est le seul geste nécessaire, une seule fois."
)
OFFLINE_MSG = "Le téléphone est « offline » pour adb : débranche puis rebranche le câble."


def find_scrcpy_dir() -> Path:
    candidates = []
    env = os.environ.get("GHOSTTHUMB_SCRCPY_DIR")
    if env:
        candidates.append(Path(env))
    candidates.append(ROOT / "scrcpy")
    candidates.extend(sorted((Path.home() / "Downloads").glob("scrcpy-win64-v*/scrcpy-win64-v*"), reverse=True))
    for c in candidates:
        if (c / "scrcpy-server").is_file():
            return c
    raise PhoneError("scrcpy introuvable : mets le dossier scrcpy dans GhostThumb\\scrcpy ou définis GHOSTTHUMB_SCRCPY_DIR.")


def scrcpy_version(scrcpy_dir: Path) -> str:
    """Version exacte attendue par scrcpy-server (il refuse tout client d'une autre version)."""
    try:
        out = subprocess.run(
            [str(scrcpy_dir / f"scrcpy{EXE}"), "--version"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=15, creationflags=NO_WINDOW,
        ).stdout.decode("utf-8", "replace")
        m = re.match(r"scrcpy (\S+)", out.strip())
        if m:
            return m.group(1)
    except (OSError, subprocess.TimeoutExpired):
        pass
    m = re.search(r"v(\d+\.\d+(?:\.\d+)?)", str(scrcpy_dir))
    return m.group(1) if m else DEFAULT_SCRCPY_VERSION


@dataclass
class Device:
    serial: str
    state: str
    info: dict = field(default_factory=dict)

    @property
    def is_tcp(self) -> bool:
        return ":" in self.serial or "._adb-tls-connect" in self.serial


def parse_devices(output: str) -> list[Device]:
    devices = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*", "adb server")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        devices.append(Device(parts[0], parts[1], info))
    return devices


def choose_device(devices: list[Device], wanted: str = "") -> Device:
    if wanted:
        for d in devices:
            if d.serial == wanted:
                if d.state == "device":
                    return d
                raise PhoneError(UNAUTHORIZED_MSG if d.state == "unauthorized" else OFFLINE_MSG)
        raise PhoneError(f"Le téléphone {wanted} (choisi dans config.json) n'est pas connecté.")
    ready = [d for d in devices if d.state == "device"]
    if not ready:
        states = {d.state for d in devices}
        if "unauthorized" in states:
            raise PhoneError(UNAUTHORIZED_MSG)
        if "offline" in states:
            raise PhoneError(OFFLINE_MSG)
        raise PhoneError(NO_DEVICE_MSG)
    usb = [d for d in ready if not d.is_tcp]
    return (usb or ready)[0]


class Adb:
    def __init__(self, scrcpy_dir: Path | None = None):
        self.scrcpy_dir = scrcpy_dir or find_scrcpy_dir()
        exe = self.scrcpy_dir / f"adb{EXE}"
        if not exe.is_file():
            raise PhoneError(f"adb introuvable dans {self.scrcpy_dir}.")
        self.path = str(exe)
        self._server_started = False

    def start_server(self) -> None:
        # Premier lancement du démon adb sans tubes : sous Windows, le démon hérite des tubes
        # de son parent et un appel capturé pourrait alors ne jamais rendre la main.
        if self._server_started:
            return
        try:
            subprocess.run([self.path, "start-server"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise PhoneError("Le démon adb ne démarre pas (délai dépassé).") from None
        self._server_started = True

    def run(self, args: list[str], serial: str | None = None, timeout: float = 20, check: bool = True,
            binary: bool = False) -> str | bytes:
        self.start_server()
        cmd = [self.path] + (["-s", serial] if serial else []) + list(args)
        try:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                                  creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise PhoneError(f"adb {' '.join(args)[:60]} : délai dépassé ({timeout:g} s).") from None
        if check and proc.returncode != 0:
            msg = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
            raise PhoneError(f"adb {args[0]} a échoué : {msg[:400]}")
        return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")

    def devices(self) -> list[Device]:
        return parse_devices(self.run(["devices", "-l"]))

    def shell(self, serial: str, command: str, timeout: float = 20, check: bool = False) -> str:
        return self.run(["shell", command], serial=serial, timeout=timeout, check=check)

    def exec_out(self, serial: str, command: str, timeout: float = 30) -> bytes:
        return self.run(["exec-out", command], serial=serial, timeout=timeout, binary=True, check=False)
