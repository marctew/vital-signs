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
- Not checked: that a site login made over VNC survives a reboot. Not needed for the current content.

## Other findings

- **Scaled screenshots flash.** `Page.captureScreenshot` with a `clip` and `scale` makes Chromium re-lay the page out, which showed on the real screens as a white flash about two seconds after each playlist switch. Capturing without clip or scale fixed it, so screenshots are full resolution.
- **Chromium cannot hide the pointer.** Under XWayland on labwc, CSS `cursor: none` does not reach the physical pointer, even after real mouse movement. The cursor is hidden at the compositor instead: `swayidle` runs `wlrctl pointer move 10000 10000` after `cursor_hide_seconds`, parking the pointer in the bottom-right corner of the layout.
- **Poll timeouts.** The agent logs "Server unreachable (timed out)" every minute or two and reconnects within a few seconds. The screens are unaffected. The Pi was on Wi-Fi at the time and is moving to Ethernet; not investigated further. Look again if it still happens on Ethernet.

## Later features

Status of everything added after v1, as of 2026-10-04. "Confirmed" means Marc reported it working on the real Pi, monitors or services. "Not confirmed" means it has only been tested off the device (automated tests, headless Chrome, or simulated data) and nobody has yet reported seeing it work for real.

| Feature | Status | Notes |
|---|---|---|
| Next / Previous | Confirmed | |
| VNC here | Confirmed | Needs `sudo -u vnc`; see section 6 |
| Idle cursor hiding | Confirmed | `swayidle` + `wlrctl` |
| Screen power off and on (`wlopm`) | Confirmed by hand | Marc ran `wlopm --off/--on` and both monitors behaved |
| Screen power schedules, multi-stage | Not confirmed | The switch at a real schedule boundary has not been reported |
| Manual power override | Not confirmed | In particular the hand-back at the next scheduled change |
| Playlists by time | Not confirmed | The switch at a real schedule boundary |
| Update all | Not confirmed | Whether each service restarts itself cleanly |
| Admin login and control token | Not confirmed | |
| Split screens | Confirmed | Grid layout and the editor were used on the real setup |
| Frame-blocking header removal in splits | Not confirmed on the Pi | Worked in desktop Chrome against a site that forbids framing |
| Clock, weather, RSS modules | Seen in use | Marc reported a fault in the clock (fixed) and asked for changes, so they run; sizes on the monitors not reviewed |
| Plex now playing | Confirmed | Against the real Plex server |
| Plex recently added, synopsis, compact layout | Not confirmed | Tested with sample posters and simulated server replies |
| Calendar module | Not confirmed | Tested with a generated calendar only |
| Frigate cameras | Not confirmed | No Frigate server was available: live video, pictures and one-at-a-time cycling are all untested for real |
| Per-display page, content previews | Not confirmed | Built and viewed in a desktop browser against test data |
| Backups and restore | Not confirmed | Round trip is covered by the tests; the nightly timer and `restore.sh` have not run on the LXC |

Open question from v1: the agent's poll timeouts were on Wi-Fi and are expected to stop on Ethernet.
