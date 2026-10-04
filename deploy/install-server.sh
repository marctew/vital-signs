#!/usr/bin/env bash
# Install the Vital Signs server on an Ubuntu LXC. Safe to re-run.
#   bash deploy/install-server.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$(id -u)" -ne 0 ]; then
    exec sudo bash "$0" "$@"
fi
RUN_USER="${SUDO_USER:-root}"
as_user() { if [ "$RUN_USER" = root ]; then "$@"; else sudo -u "$RUN_USER" "$@"; fi; }

CONF=/etc/vitalsigns/server.toml
DATA=/var/lib/vitalsigns

echo "==> Installing packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv git

python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || { echo "Python 3.11 or newer is required (Ubuntu 24.04 or later)." >&2; exit 1; }

echo "==> Python environment"
[ -x "$REPO/.venv/bin/python" ] || as_user python3 -m venv "$REPO/.venv"
as_user "$REPO/.venv/bin/pip" install -q --upgrade pip
as_user "$REPO/.venv/bin/pip" install -q -r "$REPO/server/requirements.txt"

echo "==> Config and data"
mkdir -p /etc/vitalsigns "$DATA"
if [ ! -f "$CONF" ]; then
    TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    sed "s|CHANGE_ME|$TOKEN|" "$REPO/server/config.example.toml" > "$CONF"
    echo "    Wrote $CONF with a new agent token"
fi
chmod 600 "$CONF"
chown "$RUN_USER" "$CONF"
chown -R "$RUN_USER" "$DATA"

echo "==> systemd service"
sed -e "s|@REPO@|$REPO|g" -e "s|@USER@|$RUN_USER|g" "$REPO/deploy/vitalsigns-server.service" \
    > /etc/systemd/system/vitalsigns-server.service
systemctl daemon-reload
systemctl enable -q vitalsigns-server
systemctl restart vitalsigns-server

PORT="$(sed -n 's/^port *= *\([0-9]*\).*/\1/p' "$CONF")"
echo
echo "Vital Signs server is running: http://$(hostname -I | awk '{print $1}'):${PORT:-8080}"
echo "Agents need the agent_token from $CONF"
echo "Logs: journalctl -u vitalsigns-server -f"
