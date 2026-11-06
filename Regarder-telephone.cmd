@echo off
rem Affiche le telephone en direct sur le PC ; la souris permet de reprendre la main.
rem Fermer cette fenetre n'arrete pas l'IA : elle pilote par son propre canal.
cd /d "%~dp0scrcpy"
start "" wscript.exe scrcpy-noconsole.vbs --window-title "Telephone - GhostThumb" --no-audio --stay-awake %*
