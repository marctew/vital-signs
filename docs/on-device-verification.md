# On-device verification

Findings from the first Pi (`vital-sign-desk`, Raspberry Pi 5, Raspberry Pi OS desktop, two LG ultrawides in portrait), checked on 2026-10-04. DESIGN.md marks these items **Verify on device**.

Not recorded yet: the exact OS, labwc and Chromium versions. Run `python -m agent probe` on the Pi and add them here.

## 1. Connector names

- Result: the two monitors are `HDMI-A-1` and `HDMI-A-2`, as the example config assumes.
- On this Pi `HDMI-A-1` is at 0,0 and `HDMI-A-2` at 1080,0, each 1080 x 2560 after rotation.

## 2. Rotation and position (kanshi)

- Result: works. The kanshi profile written by `deploy/install-agent.sh` is applied at boot, and the agent reads both outputs as 1080 x 2560 portrait at the configured positions.
- Not checked: whether `wlr-randr --json` is available or the agent is using its plain-text fallback. Either way detection works.

## 3. Window placement

- Result: the preferred approach works. With `placement = "xwayland"` Chromium honours `--window-position`, and the bounds read back over CDP match the output:
  `{'left': 1080, 'top': 0, 'width': 1080, 'height': 2560, 'windowState': 'fullscreen'}` and
  `{'left': 0, 'top': 0, 'width': 1080, 'height': 2560, 'windowState': 'fullscreen'}`.
- Both windows land on the correct monitor after a cold boot and after Chromium is killed.
- The `labwc-rules` fallback has not been needed and is untested on a device.

## 4. Agent start-up

- Result: works. The systemd user unit started from `~/.config/labwc/autostart` brings both screens up after a cold boot with no keyboard attached.
- The agent's log is in the system journal, not a per-user one: `journalctl --user-unit vitalsigns-agent`. `journalctl --user -u …` reports "No journal files were found".

## 5. Zoom

- Result: `zoom_method = "emulation"` works. A zoomed item fills the screen and is sharp.

## 6. wayvnc

- Result: works alongside XWayland placement, using TigerVNC Viewer. TightVNC cannot connect ("No security types supported").
- wayvnc shows one output at a time. Raspberry Pi OS runs it as a system service under the `vnc` user with its control socket at `/tmp/wayvnc/wayvncctl.sock`, writable only by that user. Switching outputs therefore needs
  `sudo -n -u vnc wayvncctl --socket=/tmp/wayvnc/wayvncctl.sock output-set <connector>`,
  which is what the dashboard's **VNC here** button makes the agent run.
- Not checked: that a site login made over VNC survives a reboot.

## Other findings

- **Scaled screenshots flash.** `Page.captureScreenshot` with a `clip` and `scale` makes Chromium re-lay the page out, which showed on the real screens as a white flash about two seconds after each playlist switch. Capturing without clip or scale fixed it, so screenshots are full resolution.
- **Chromium cannot hide the pointer.** Under XWayland on labwc, CSS `cursor: none` does not reach the physical pointer, even after real mouse movement. The cursor is hidden at the compositor instead: `swayidle` runs `wlrctl pointer move 10000 10000` after `cursor_hide_seconds`, parking the pointer in the bottom-right corner of the layout.
- **Poll timeouts.** The agent logs "Server unreachable (timed out)" every minute or two and reconnects within a few seconds. The screens are unaffected. Cause not yet investigated.
