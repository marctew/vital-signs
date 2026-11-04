#!/usr/bin/env bash
# Deploy the Vital Signs server from this checkout: install dependencies if they changed, check the
# code compiles, restart, wait for /healthz. (Pis use deploy/update.sh, which calls this on the server.)
#
#   scripts/update.sh            restart now; refuses if /healthz says something is busy
#   scripts/update.sh --pull     git pull first
#   scripts/update.sh --force    restart even if something is busy
#   scripts/update.sh --if-idle  restart only if nothing is busy; otherwise leave the new code
#                                for the next restart and still succeed (the design updater uses this)
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

SERVICE=vitalsigns-server
CONF="${VITALSIGNS_CONFIG:-/etc/vitalsigns/server.toml}"
PORT="$(sed -n 's/^port *= *\([0-9]*\).*/\1/p' "$CONF" 2>/dev/null || true)"
HEALTH="http://127.0.0.1:${PORT:-8080}/healthz"
PULL=0 FORCE=0 IDLE=0
for a in "$@"; do
    case "$a" in
        --pull) PULL=1 ;;
        --force) FORCE=1 ;;
        --if-idle) IDLE=1 ;;
        *) echo "Unknown option: $a" >&2; exit 2 ;;
    esac
done
as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

if [ "$PULL" = 1 ]; then
    git pull --ff-only -q
fi

# Dependencies, only when server/requirements.txt has changed since the last install.
STAMP=.venv/.server-requirements.sha256
WANT="$(sha256sum server/requirements.txt | cut -d' ' -f1)"
if [ "$(cat "$STAMP" 2>/dev/null)" != "$WANT" ]; then
    .venv/bin/pip install -q -r server/requirements.txt
    echo "$WANT" > "$STAMP"
fi

.venv/bin/python -m compileall -q server shared wsgi.py >/dev/null \
    || { echo "The code doesn't compile; not restarting." >&2; exit 1; }

busy="$(curl -fsS -m 5 "$HEALTH" 2>/dev/null \
    | .venv/bin/python -c 'import json, sys; print(", ".join(json.load(sys.stdin).get("busy", [])))' 2>/dev/null || true)"
if [ -n "$busy" ] && [ "$FORCE" = 0 ]; then
    if [ "$IDLE" = 1 ]; then
        echo "Busy ($busy), so not restarting. The new code loads on the next restart."
        exit 0
    fi
    echo "Busy ($busy). Try again when it's finished, or use --force." >&2
    exit 1
fi

as_root systemctl restart "$SERVICE"
for _ in $(seq 60); do
    if curl -fsS -m 2 "$HEALTH" >/dev/null 2>&1; then
        echo "$SERVICE restarted at $(git rev-parse --short HEAD) and healthy."
        exit 0
    fi
    sleep 1
done
echo "$SERVICE didn't answer $HEALTH within a minute:" >&2
as_root journalctl -u "$SERVICE" -n 30 --no-pager >&2
exit 1
