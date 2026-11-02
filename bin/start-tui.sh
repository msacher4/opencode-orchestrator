#!/usr/bin/env bash
# Lance le TUI opencode ATTACHÉ au serveur de fond unique (127.0.0.1:4097).
#
# Pourquoi : engine.py (mcp/opencode-recurring) n'a aucun port en dur, il résout
# l'URL du service via $XDG_STATE_HOME/opencode/service.json. Un TUI lancé en
# --standalone démarre son propre serveur stdio : il lit la même base SQLite mais
# ne reçoit aucun événement des runs récurrents -> onglet figé sur une session
# que son serveur ne sert plus. Ce script s'attache désormais explicitement au
# serveur systemd 4097 et garde le TUI de l'option --standalone.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
BIN="$PROJECT/Opencodev2/opencode-v2"
SERVER_URL="${OPENCODE_SERVER_URL:-http://127.0.0.1:4097}"
V2_ENV_FILE="/home/marcs/.telegram-bridge-venv/v2.env"

# Les XDG_* / OPENCODE_DB / OPENCODE_DISABLE_PROJECT_CONFIG sont posés par le
# lanceur du dépôt ($BIN) : plus de duplication ici, ce script ne fait que le garde.

# --- garde : --standalone coupe le TUI des événements du service --------------
for arg in "$@"; do
  case "$arg" in
    --standalone|--standalone=*)
      cat >&2 <<'EOF'
start-tui.sh : option refusée (--standalone).

Un TUI en --standalone démarre son propre serveur privé au lieu de s'attacher au
service de fond : il coupe le TUI des événements du service, donc des runs
récurrents lancés par engine.py. L'onglet reste figé sur une session que le
service ne sert plus.

Lancez simplement :  ./tools/start-tui.sh
EOF
      exit 2
      ;;
  esac
done

# --- séparation --dry-run / arguments transmis au binaire ---------------------
DRY_RUN=0
ALL=()
for arg in "$@"; do
  if [ "$arg" = "--dry-run" ]; then
    DRY_RUN=1
  else
    ALL+=("$arg")
  fi
done

# --- extraction de --server (garde) -------------------------------------------
# Un --server accepté est retiré des ARGS (le script l'ajoute lui-même).
server_value=""
server_seen=0
prev_was_server=0
KEEP=()
for arg in "${ALL[@]+"${ALL[@]}"}"; do
  if [ "$prev_was_server" -eq 1 ]; then
    server_value="$arg"
    server_seen=1
    prev_was_server=0
    continue
  fi
  case "$arg" in
    --server=*)
      server_value="${arg#--server=}"
      server_seen=1
      prev_was_server=0
      continue
      ;;
    --server)
      server_seen=1
      prev_was_server=1
      continue
      ;;
  esac
  KEEP+=("$arg")
done

# --- garde : --server ne vaut que le serveur de fond unique -------------------
if [ "$server_seen" -eq 1 ]; then
  if [ ! "$server_value" = "$SERVER_URL" ] && [ ! "${server_value%/}" = "$SERVER_URL" ]; then
    cat >&2 <<EOF
start-tui.sh : option refusée (--server "$server_value").

L'URL fournie ne correspond pas au serveur de fond unique ($SERVER_URL).

Le TUI s'attache lui-même au serveur de fond ; aucune URL à indiquer.

Lancez simplement :  ./tools/start-tui.sh
EOF
    exit 2
  fi
fi

# --- attachement au serveur de fond unique (4097) : auth + sonde -------------
# Le mot de passe est lu dans v2.env et transmis par l'environnement, jamais par
# argv et jamais affiché. Sonde obligatoire avant tout lancement.
if [ ! -r "$V2_ENV_FILE" ]; then
  echo "start-tui.sh : $V2_ENV_FILE illisible — impossible de lire le mot de passe du serveur $SERVER_URL." >&2
  exit 1
fi

auth_line="$(grep -m1 '^OPENCODE_SERVER_PASSWORD=' "$V2_ENV_FILE" || true)"
if [ -z "$auth_line" ]; then
  echo "start-tui.sh : OPENCODE_SERVER_PASSWORD absente de $V2_ENV_FILE — mot de passe du serveur $SERVER_URL introuvable." >&2
  exit 1
fi

pass="${auth_line#OPENCODE_SERVER_PASSWORD=}"
pass="${pass%$'\r'}"
pass="${pass#\"}"; pass="${pass%\"}"
pass="${pass#\'}"; pass="${pass%\'}"

if [ -z "$pass" ]; then
  echo "start-tui.sh : mot de passe vide dans $V2_ENV_FILE — vérifie la ligne OPENCODE_SERVER_PASSWORD." >&2
  exit 1
fi

escaped_pass="${pass//\\/\\\\}"; escaped_pass="${escaped_pass//\"/\\\"}"
user_line="user = \"opencode:${escaped_pass}\""
code="$(printf '%s\n' "$user_line" | curl -s -o /dev/null -w '%{http_code}' --max-time 5 -K - "$SERVER_URL/api/info" 2>/dev/null || true)"

case "$code" in
  200)
    ;;
  401)
    echo "start-tui.sh : le serveur $SERVER_URL refuse le mot de passe lu dans $V2_ENV_FILE (401)." >&2
    echo "start-tui.sh : mot de passe du pont et mot de passe du serveur différents — à aligner dans $V2_ENV_FILE ou côté serveur, rien n'a été lancé." >&2
    exit 1
    ;;
  *)
    echo "start-tui.sh : le serveur $SERVER_URL ne répond pas (code $code) — relance opencode-serve-v2." >&2
    exit 1
    ;;
esac

# --- --dry-run : on affiche, on ne lance RIEN ---------------------------------
if [ "$DRY_RUN" -eq 1 ]; then
  printf 'start-tui.sh : dry-run — aucun processus lancé.\n'
  printf 'PROJECT  : %s\n' "$PROJECT"
  printf 'binaire  : %s\n' "$BIN"
  printf 'commande :'
  printf ' %q' "$BIN" --server "$SERVER_URL" "${KEEP[@]+"${KEEP[@]}"}"
  printf '\n'
  printf 'serveur  : %s (%s)\n' "$SERVER_URL" "$code"
  exit 0
fi

# --- lancement : TUI attaché au serveur de fond unique -----------------------
exec OPENCODE_PASSWORD="$pass" "$BIN" --server "$SERVER_URL" "${KEEP[@]+"${KEEP[@]}"}"
