#!/usr/bin/env bash
# Install the Vital Signs agent on a Raspberry Pi (Raspberry Pi OS desktop, labwc).
# Run as the desktop user, not root. Safe to re-run: run it again after editing agent.toml.
#   bash deploy/install-agent.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ "$(id -u)" -ne 0 ] || { echo "Run this as the desktop user, not root." >&2; exit 1; }

CONF="$HOME/.config/vitalsigns/agent.toml"
UNIT="$HOME/.config/systemd/user/vitalsigns-agent.service"
KANSHI="$HOME/.config/kanshi/config"
AUTOSTART="$HOME/.config/labwc/autostart"
PY="$REPO/.venv/bin/python"

echo "==> Installing packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv git wlr-randr kanshi wayvnc swayidle wlrctl
if ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null; then
    sudo apt-get install -y -qq chromium || sudo apt-get install -y -qq chromium-browser
fi

echo "==> Python environment"
[ -x "$PY" ] || python3 -m venv "$REPO/.venv"
"$REPO/.venv/bin/pip" install -q --upgrade pip
"$REPO/.venv/bin/pip" install -q -r "$REPO/agent/requirements.txt"

if [ ! -f "$CONF" ]; then
    mkdir -p "$(dirname "$CONF")"
    cp "$REPO/agent/config.example.toml" "$CONF"
    chmod 600 "$CONF"
    echo
    echo "Created $CONF"
    echo "Set server_url, token and one [[outputs]] block per monitor, then run this script again."
    echo
    echo "Outputs detected right now:"
    wlr-randr 2>/dev/null || echo "  (none: run 'wlr-randr' in a terminal on the Pi's desktop to list them)"
    exit 0
fi

cd "$REPO"
if grep -q 'CHANGE_ME' "$CONF"; then
    echo "Edit $CONF first: token is still CHANGE_ME." >&2
    exit 1
fi

echo "==> Pinning output position and rotation (kanshi)"
mkdir -p "$(dirname "$KANSHI")"
RENDERED="$("$PY" -m agent render-kanshi)"
if [ -f "$KANSHI" ] && ! grep -q 'Written by Vital Signs' "$KANSHI"; then
    cp "$KANSHI" "$KANSHI.before-vitalsigns"
    echo "    Existing kanshi config saved as $KANSHI.before-vitalsigns"
fi
printf '%s\n' "$RENDERED" > "$KANSHI"

echo "==> systemd user service"
mkdir -p "$(dirname "$UNIT")"
sed -e "s|@REPO@|$REPO|g" "$REPO/deploy/vitalsigns-agent.service" > "$UNIT"
systemctl --user daemon-reload

echo "==> Start with the graphical session (labwc autostart)"
mkdir -p "$(dirname "$AUTOSTART")"
touch "$AUTOSTART"
if ! grep -q 'vitalsigns-agent' "$AUTOSTART"; then
    cat >> "$AUTOSTART" <<'EOF'

# Vital Signs: hand the session environment to systemd, then start the agent.
systemctl --user import-environment WAYLAND_DISPLAY DISPLAY XDG_CURRENT_DESKTOP
systemctl --user restart vitalsigns-agent.service &
EOF
fi

echo "==> Desktop autologin, VNC, no screen blanking"
if command -v raspi-config >/dev/null; then
    sudo raspi-config nonint do_boot_behaviour B4 || echo "    Could not enable desktop autologin; set it in raspi-config"
    sudo raspi-config nonint do_vnc 0 || echo "    Could not enable wayvnc; set it in raspi-config"
    sudo raspi-config nonint do_blanking 1 || echo "    Could not disable screen blanking; set it in raspi-config"
else
    echo "    raspi-config not found: enable desktop autologin and wayvnc by hand"
fi

if grep -q '^placement *= *"labwc-rules"' "$CONF"; then
    echo
    echo "placement is labwc-rules: merge this into ~/.config/labwc/rc.xml, then run 'labwc --reconfigure':"
    "$PY" -m agent render-labwc-rules
fi

echo
if [ -n "${WAYLAND_DISPLAY:-}" ]; then
    kanshictl reload 2>/dev/null || true
    systemctl --user import-environment WAYLAND_DISPLAY DISPLAY XDG_CURRENT_DESKTOP
    systemctl --user restart vitalsigns-agent.service
    echo "Agent started. Logs: journalctl --user-unit vitalsigns-agent -f"
else
    echo "Installed. Reboot the Pi to start the agent: sudo reboot"
fi
