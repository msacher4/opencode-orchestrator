#!/usr/bin/env bash
# Installation idempotente des services systemd pour telegram-bridge v2
# Doit être lancé en root (sudo). Ne contient AUCUN secret.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERREUR : ce script doit être lancé en root (sudo ./install.sh)" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="/etc/systemd/system"

echo "=== Copie des units systemd ==="
if [[ ! -f "$SCRIPT_DIR/units/telegram-bridge.service" || ! -f "$SCRIPT_DIR/units/opencode-serve-v2.service" ]]; then
    echo "ERREUR : un ou plusieurs fichiers unit sources manquent dans $SCRIPT_DIR/units/" >&2
    exit 1
fi
cp -v "$SCRIPT_DIR/units/telegram-bridge.service" "$UNIT_DIR/"
cp -v "$SCRIPT_DIR/units/opencode-serve-v2.service" "$UNIT_DIR/"

# S'assurer que bridge.env a les bons droits (600)
chmod 600 "$SCRIPT_DIR/bridge.env"
chown marcs:marcs "$SCRIPT_DIR/bridge.env"

echo "=== systemctl daemon-reload ==="
systemctl daemon-reload

echo "=== Activation et démarrage opencode-serve-v2 ==="
systemctl enable --now opencode-serve-v2.service

echo "=== Redémarrage telegram-bridge ==="
systemctl restart telegram-bridge.service

echo "=== État des services ==="
for svc in opencode-serve.service opencode-serve-v2.service telegram-bridge.service; do
    echo -n "$svc: "
    systemctl is-active "$svc" 2>/dev/null || echo "inactive/failed"
done