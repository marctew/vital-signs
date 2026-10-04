# Vital Signs

Self-hosted digital signage: a Flask server controls what each monitor on a Raspberry Pi shows. Read DESIGN.md before changing behaviour; it records decisions that are already made.

## Layout

- `server/` Flask + SQLite (plain `sqlite3`, schema in `server/db.py`), server-rendered Jinja templates, vanilla JS. No build step, no ORM.
- `agent/` Pi-side agent. `player.py` is one thread per display and the only code that talks to Chromium (over CDP, `cdp.py`). `browser.py` launches and places the window. `outputs.py` reads `wlr-randr` and renders kanshi / labwc config.
- `shared/` `protocol.py` (PROTOCOL_VERSION) and `API.md`, the contract both sides follow.
- `pages/` local pages. Folders starting with `_` are shared assets, not pages.
- `deploy/` install and update scripts, systemd unit templates (`@REPO@`, `@USER@` placeholders).

## Rules

- Both programs run from the repo root: `python -m server`, `python -m agent`. Python 3.11+.
- REST only between agent and server. No WebSocket, no MQTT. The agent polls.
- Content is never iframed. Everything the agent shows is a URL in its own tab.
- UI routes and the control API both go through `server/services.py`. Add operations there, not in the route.
- Changing the poll request or response means updating `shared/API.md` and, if old peers would break, bumping `PROTOCOL_VERSION`.
- Nothing may assume two displays, portrait, or 1080 x 2560.
- Runtime data, config with secrets and Chromium profiles stay out of the tracked tree.
- v2 items in DESIGN.md section 6 are not to be built yet, but must not be blocked.
- Anything marked "Verify on device" gets its findings written to `docs/on-device-verification.md`.

## Testing without a Pi

The agent runs against desktop Chrome with `detect_outputs = false`, `placement = "none"`, per-output `width`/`height` and `extra_chromium_args = ["--headless=new"]`. See the Development section of README.md.
