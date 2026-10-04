# On-device verification

DESIGN.md marks several things **Verify on device**. None of them have been checked on the real Pi yet: the code was built and tested on a development machine against headless Chrome, which exercises CDP, playback, overrides, failure handling and the server, but not the compositor.

Work through this list on the Pi and replace each "Result" with what you found. `python -m agent probe` prints most of the facts in one go (run it from a terminal on the Pi's desktop, or over SSH with `WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/$(id -u)` set).

## 1. Connector names

- Check: `wlr-randr` lists both monitors. Note which connector is physically the left one.
- The example config assumes `HDMI-A-1` (left) and `HDMI-A-2` (right).
- Result: _not yet verified_

## 2. Rotation and position (kanshi)

- `deploy/install-agent.sh` writes `~/.config/kanshi/config` from `agent.toml`.
- Check: after a reboot `wlr-randr` shows each output with the configured transform and position, and the picture is the right way up. If it is upside down, use `rotation = 270` instead of `90`.
- Check: kanshi is actually running in the session (`pgrep kanshi`). If Raspberry Pi OS no longer starts it, add `kanshi &` to `~/.config/labwc/autostart`.
- Check: does `wlr-randr --json` work? The agent falls back to parsing the plain output if not.
- Result: _not yet verified_

## 3. Window placement

Preferred: `placement = "xwayland"`. Chromium runs with `--ozone-platform=x11 --kiosk --window-position=X,Y`, and the agent reads the bounds back with `Browser.getWindowForTarget`, relaunching up to four times if they are wrong.

- Check: the agent log shows `window placed: {...}` for each display and each window is fullscreen on the right monitor.
- Check: left is still left after several cold boots.
- If windows land on the wrong monitor or are not fullscreen, switch to `placement = "labwc-rules"`, re-run `deploy/install-agent.sh`, merge the printed rules into `~/.config/labwc/rc.xml` and run `labwc --reconfigure`. In this mode Chromium is a native Wayland client, its app id comes from `--class`, and position cannot be read back over CDP, so the agent only checks the window size.
- Result (which approach works, OS / labwc / Chromium versions): _not yet verified_

## 4. Agent start-up

- The agent is a systemd user unit started from `~/.config/labwc/autostart`, after `systemctl --user import-environment`.
- Check: after a cold boot with no keyboard, `systemctl --user status vitalsigns-agent` is active and both screens show content.
- Check: labwc still runs the system autostart (panel, kanshi) as well as the user one. Raspberry Pi OS starts labwc with config merging; if it does not on your version, copy the lines you need from `/etc/xdg/labwc/autostart`.
- Result: _not yet verified_

## 5. Zoom

- `zoom_method = "emulation"` uses `Emulation.setDeviceMetricsOverride` with a scale, which behaves like browser zoom.
- Check: a content item with zoom 1.5 fills the screen, is sharp, and lays out as if the screen were narrower. If it leaves a blank area or looks blurry, set `zoom_method = "css"`.
- Result: _not yet verified_

## 6. wayvnc

- `raspi-config nonint do_vnc 0` enables wayvnc.
- Check: a VNC client can connect, see both monitors (or switch between outputs) and type into the kiosk windows, so sites can be logged in to once. Logins persist in `~/.local/state/vitalsigns/profiles/<name>`.
- Check: this works with the placement approach chosen in step 3.
- Result: _not yet verified_
