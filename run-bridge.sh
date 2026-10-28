#!/usr/bin/env bash
# Lance le bridge Telegram → OpenCode hors systemd (dépannage).
# Le service système /etc/systemd/system/telegram-bridge.service reste la voie
# normale (au reboot il démarre tout seul avec la version patchée de bridge.py).
set -euo pipefail

UNIT=/etc/systemd/system/telegram-bridge.service
exec 3< <(grep -oP '(?<=^Environment=TELEGRAM_TOKEN=).*' "$UNIT" | head -1)
read -r TOKEN <&3
exec 3<&-

export TELEGRAM_TOKEN="$TOKEN"
export OPENCODE_URL="http://127.0.0.1:4096"
exec /home/marcs/.telegram-bridge-venv/bin/python3 /home/marcs/.telegram-bridge-venv/bridge.py
