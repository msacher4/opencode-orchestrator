#!/usr/bin/env bash
# Lance le TUI opencode ATTACHÉ au service de fond (jamais en --standalone).
#
# Pourquoi : engine.py (mcp/opencode-recurring) n'a aucun port en dur, il résout
# l'URL du service via $XDG_STATE_HOME/opencode/service.json. Un TUI lancé en
# --standalone démarre son propre serveur stdio : il lit la même base SQLite mais
# ne reçoit aucun événement des runs récurrents -> onglet figé sur une session
# que son serveur ne sert plus. Les XDG_* sont posés par le lanceur du dépôt
# (Opencodev2/opencode-v2) ; ce script ne fait que refuser --standalone.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
BIN="$PROJECT/Opencodev2/opencode-v2"
SERVICE_JSON="$PROJECT/Opencodev2/state/opencode/service.json"

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
ARGS=()
for arg in "$@"; do
  if [ "$arg" = "--dry-run" ]; then
    DRY_RUN=1
  else
    ARGS+=("$arg")
  fi
done

# --- lecture de service.json (sans jq obligatoire) ----------------------------
service_field() {
  local field="$1" value=""
  if [ -r "$SERVICE_JSON" ] && command -v python3 >/dev/null 2>&1; then
    value="$(python3 - "$SERVICE_JSON" "$field" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    sys.exit(0)
value = data.get(sys.argv[2])
print("" if value is None else value)
PY
)"
  fi
  if [ -z "$value" ] && [ -r "$SERVICE_JSON" ]; then
    case "$field" in
      url) value="$(grep -o '"url"[[:space:]]*:[[:space:]]*"[^"]*"' "$SERVICE_JSON" |
        head -n1 | sed 's/^[^"]*"\([^"]*\)"$/\1/')" ;;
      pid) value="$(grep -o '"pid"[[:space:]]*:[[:space:]]*[0-9]\+' "$SERVICE_JSON" |
        head -n1 | grep -o '[0-9]\+$')" ;;
    esac
  fi
  printf '%s' "$value"
}

# --- garde : --server refuse toute URL sauf celle du service de fond ----------
server_value=""
server_seen=0
prev_was_server=0
for arg in "${ARGS[@]+"${ARGS[@]}"}"; do
  if [ "$prev_was_server" -eq 1 ]; then
    # cet argument est la valeur de --server (présente)
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
      ;;
    --server)
      server_seen=1
      prev_was_server=1
      ;;
  esac
done

if [ "$server_seen" -eq 1 ]; then
  if [ ! -r "$SERVICE_JSON" ]; then
    cat >&2 <<'EOF'
start-tui.sh : option refusée (--server).

Aucun service.json lisible : on ne peut pas vérifier l'URL du service de fond.

Le TUI s'attache seul au service de fond ; aucune URL à indiquer.

Lancez simplement :  ./tools/start-tui.sh
EOF
    exit 2
  fi
  service_url="$(service_field url)"
  if [ -z "$service_url" ]; then
    cat >&2 <<'EOF'
start-tui.sh : option refusée (--server).

L'URL du service de fond est vide dans service.json : on ne peut pas vérifier.

Le TUI s'attache seul au service de fond ; aucune URL à indiquer.

Lancez simplement :  ./tools/start-tui.sh
EOF
    exit 2
  fi
  if [ "${server_value%/}" != "${service_url%/}" ]; then
    cat >&2 <<EOF
start-tui.sh : option refusée (--server "$server_value").

L'URL fournie ne correspond pas au service de fond (URL du service : $service_url).

Le TUI s'attache seul au service de fond ; aucune URL à indiquer.

Lancez simplement :  ./tools/start-tui.sh
EOF
    exit 2
  fi
fi

# --- --dry-run : on affiche, on ne lance RIEN ---------------------------------
if [ "$DRY_RUN" -eq 1 ]; then
  printf 'start-tui.sh : dry-run — aucun processus lancé.\n'
  printf 'PROJECT  : %s\n' "$PROJECT"
  printf 'binaire  : %s\n' "$BIN"
  printf 'commande :'
  printf ' %q' "$BIN" ${ARGS[@]+"${ARGS[@]}"}
  printf '\n'
  if [ -r "$SERVICE_JSON" ]; then
    url="$(service_field url)"
    pid="$(service_field pid)"
    printf 'service  : url=%s pid=%s (%s)\n' "${url:-?}" "${pid:-?}" \
      "$([ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && echo vivant || echo INACTIF)"
    printf 'service.json : %s\n' "$SERVICE_JSON"
  else
    printf 'service  : pas de %s — le TUI démarrera le service au besoin.\n' "$SERVICE_JSON"
  fi
  exit 0
fi

# --- lancement : TUI attaché au service --------------------------------------
exec "$PROJECT/Opencodev2/opencode-v2" ${ARGS[@]+"${ARGS[@]}"}
