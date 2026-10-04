#!/usr/bin/env bash
# Restore the server from a backup: stops the server, replaces its data, starts it again.
#   bash deploy/restore.sh <backup.tar.gz> [--keep-config]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ $# -ge 1 ] || { echo "Usage: bash deploy/restore.sh <backup.tar.gz> [--keep-config]" >&2; exit 1; }
[ -f "$1" ] || { echo "No such file: $1" >&2; exit 1; }
if [ "$(id -u)" -ne 0 ]; then
    exec sudo bash "$0" "$@"
fi

export VITALSIGNS_CONFIG="${VITALSIGNS_CONFIG:-/etc/vitalsigns/server.toml}"
cd "$REPO"
systemctl stop vitalsigns-server
# Start the server again even if the restore fails, so the screens are not left without one.
trap 'systemctl start vitalsigns-server' EXIT
"$REPO/.venv/bin/python" -m server restore "$@"
