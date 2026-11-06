"""Serveur MCP GhostThumb : lancé par le client MCP, il donne à l'IA les outils phone_*."""

import sys

sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

from ghostthumb.mcp import serve  # noqa: E402

if __name__ == "__main__":
    serve()
