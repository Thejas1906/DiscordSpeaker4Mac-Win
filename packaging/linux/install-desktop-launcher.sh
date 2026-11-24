#!/usr/bin/env bash
# Installs an application-menu launcher for the desktop controller running from this checkout.
# Creates .venv with the desktop dependencies if missing. Docker-only users do not need this.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
[ -d "$ROOT/.venv" ] || python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install -q -r "$ROOT/src/requirements-desktop.txt"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"; ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons"
mkdir -p "$APPS" "$ICONS"
cp "$ROOT/src/ui/assets/logo.png" "$ICONS/discordspeaker.png"
sed -e "s|@EXEC@|\"$ROOT/.venv/bin/python\" \"$ROOT/src/app.py\"|" -e "s|@ICON@|$ICONS/discordspeaker.png|" \
    "$ROOT/packaging/linux/discordspeaker.desktop" > "$APPS/discordspeaker.desktop"
chmod +x "$APPS/discordspeaker.desktop"
echo "Installed: $APPS/discordspeaker.desktop  (search 'DiscordSpeaker' in your application menu)"
