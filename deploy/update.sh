#!/usr/bin/env bash
# Update this device: pull, install dependencies, restart whichever service is installed here.
#   bash deploy/update.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIP="$REPO/.venv/bin/pip"

git -C "$REPO" pull --ff-only

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
