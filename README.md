# Vital Signs

Self-hosted digital signage. A web UI on a server controls which web pages are shown on each monitor attached to a Raspberry Pi.

- `server/` Flask app: admin UI, control API, agent API, local pages, data proxy.
- `agent/` Python service on the Pi: one kiosk Chromium per monitor, driven over the Chrome DevTools Protocol.
- `shared/` protocol version and the API contract ([shared/API.md](shared/API.md)).
- `pages/` version-controlled local pages, one folder each.
- `deploy/` install and update scripts, systemd units.
- `docs/` on-device verification notes.

The design is in [DESIGN.md](DESIGN.md). Both devices clone the whole repo and run only their own install script. Python 3.11 or newer is needed on both.

## Install the server (Ubuntu LXC)

```bash
git clone https://github.com/marctew/vital-signs.git
cd vital-signs
bash deploy/install-server.sh
```

This creates a virtualenv in `.venv`, writes `/etc/vitalsigns/server.toml` with a fresh agent token, keeps data in `/var/lib/vitalsigns` and starts the `vitalsigns-server` systemd service on port 8080. Point Zoraxy at that port.

## Install the agent (Raspberry Pi OS desktop)

Run as the desktop user, not root.

```bash
git clone https://github.com/marctew/vital-signs.git
cd vital-signs
bash deploy/install-agent.sh
```

The first run installs packages and creates `~/.config/vitalsigns/agent.toml`. Edit it:

- `server_url` and `token` (the `agent_token` from the server's `server.toml`)
- one `[[outputs]]` block per monitor: connector (from `wlr-randr`), friendly name, rotation, position

Then run the script again. It pins the output layout in kanshi, installs the systemd user service, starts it with the graphical session, and enables desktop autologin and wayvnc. Reboot to check that everything comes up unattended.

A Pi with one landscape screen is a config with a single `[[outputs]]` block and `rotation = 0`.

Before relying on it, work through [docs/on-device-verification.md](docs/on-device-verification.md).

## Update either device

```bash
bash deploy/update.sh
```

Pulls, installs dependencies and restarts whichever service is installed on that device. Nothing restarts when there is nothing new (`--force` restarts anyway).

**Update all** on the dashboard does the same on the server and every Pi at once. New system packages are not installed this way: when a release needs one, re-run the install script on that device. The dashboard flags an agent whose git SHA or protocol version differs from the server's.

## Using it

1. Open the server in a browser. Displays appear on the dashboard once their agent polls.
2. **Content**: add URLs with optional zoom, refresh interval and injected CSS. Local pages are listed automatically.
3. **Playlists**: order content items and give each a duration. One item makes a static screen.
4. **Displays**: assign a playlist, push a temporary override, identify or reload a screen, step through the playlist, set a per-display zoom, and choose which screen power schedules apply.
5. **Schedules**: define named on-periods (on time, off time, days). A display's monitor is on during any schedule assigned to it, and always on if it has none.

### Login

The admin UI is open until you set a password on the server:

```bash
cd /opt/vital-signs && .venv/bin/python -m server set-password
```

After that the UI asks for it, and the control API needs `control_token` from `server.toml` as a Bearer token. The pages shown on the displays and the agent API are not affected. `clear-password` turns the login off again.

### Local pages

A page is a folder with an `index.html`, served at `/pages/<name>/`. Add one to `pages/` in the repo and pull on the server, or upload a zip or single HTML file under **Local pages**. See [pages/clock/](pages/clock/index.html) for an example.

- `/pages/_shared/base.css` is a responsive base stylesheet sized in viewport units.
- `/pages/_shared/vitalsigns.js` exposes `VS.params`, `VS.display` and `VS.data(source)`.
- The agent adds `display`, `width`, `height` and `orientation` query parameters to local pages. Add your own in the content item's URL, for example `/pages/clock/?tz=Europe/London`.
- Pages fetch external data through the data proxy: define a named source under `[data_sources.<name>]` in `server.toml` and call `VS.data('<name>')`.

### Control API

For Home Assistant and n8n. Full contract in [shared/API.md](shared/API.md).

```bash
curl -X POST http://vitalsigns.lan:8080/api/v1/displays/signage-pi:left/override \
  -H 'Content-Type: application/json' -d '{"url": "https://example.com", "minutes": 5}'
```

## Development

Without a config file the server keeps its data in `./data` (gitignored):

```bash
python -m venv .venv
.venv/bin/pip install -r server/requirements.txt -r agent/requirements.txt
VITALSIGNS_CONFIG=dev-server.toml .venv/bin/python -m server
```

The agent can run against any desktop Chrome or Chromium without a Wayland compositor: in its config set `detect_outputs = false`, `placement = "none"`, give each output a `width` and `height`, and optionally `extra_chromium_args = ["--headless=new"]`. Then `python -m agent --config dev-agent.toml`.

## Logs

```bash
journalctl -u vitalsigns-server -f
```

```bash
journalctl --user-unit vitalsigns-agent -f
```
