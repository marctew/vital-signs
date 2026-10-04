#!/usr/bin/env bash
# Update this device: pull, install dependencies, restart whichever service is installed here.
# Nothing is restarted when the pull brings no changes, unless --force is given.
#   bash deploy/update.sh [--force]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIP="$REPO/.venv/bin/pip"
HOME="${HOME:-$(getent passwd "$(id -u)" | cut -d: -f6)}"

BEFORE="$(git -C "$REPO" rev-parse HEAD)"
git -C "$REPO" pull --ff-only
if [ "$BEFORE" = "$(git -C "$REPO" rev-parse HEAD)" ] && [ "${1:-}" != "--force" ]; then
    echo "$(date '+%F %T') Already up to date at $(git -C "$REPO" rev-parse --short HEAD)"
    exit 0
fi

if [ -f /etc/systemd/system/vitalsigns-server.service ]; then
    "$PIP" install -q -r "$REPO/server/requirements.txt"
    if [ "$(id -u)" -eq 0 ]; then
        systemctl restart vitalsigns-server
    else
        sudo systemctl restart vitalsigns-server
    fi
    echo "Server restarted at $(git -C "$REPO" rev-parse --short HEAD)"
fi

if [ -f "$HOME/.config/systemd/user/vitalsigns-agent.service" ]; then
    "$PIP" install -q -r "$REPO/agent/requirements.txt"
    systemctl --user restart vitalsigns-agent.service
    echo "Agent restarted at $(git -C "$REPO" rev-parse --short HEAD)"
fi
