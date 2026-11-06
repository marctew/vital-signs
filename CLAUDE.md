# Vital Signs

Self-hosted digital signage: a Flask server controls what each monitor on a Raspberry Pi shows. Read DESIGN.md before changing behaviour; it records decisions that are already made.

Built to `docs/design/APP-STANDARD.md` and styled to `docs/design/DESIGN-SPEC.md`, with home screen icons to `docs/design/PWA-ICONS.md` (from `C:/ClaudeCowork/Design Uniformity`). Follow them; no one-off styles or layouts. The home screen PNGs and `favicon.ico` in `server/static/` are made from `server/static/brand/logo.svg` by `python tools/make_icons.py server/static`: never edit them by hand. The admin UI is Home category (teal). Its own styles are in `server/static/vitalsigns.css`; `server/static/design/`, `server/templates/macros/`, `server/templates/partials/`, `server/du_flask.py` and `docs/design/` are copied in by the design updater, so never edit them here. Pages shown on the displays (`pages/`) are signage, not admin UI, and keep their own look.

## Layout

- `server/` Flask + SQLite (plain `sqlite3`, schema in `server/db.py`), server-rendered Jinja templates, vanilla JS. No build step, no ORM.
- `agent/` Pi-side agent. `player.py` is one thread per display and the only code that talks to Chromium (over CDP, `cdp.py`). `browser.py` launches and places the window. `outputs.py` reads `wlr-randr` and renders kanshi / labwc config.
- `shared/` `protocol.py` (PROTOCOL_VERSION) and `API.md`, the contract both sides follow.
- `pages/` local pages. Folders starting with `_` are shared assets, not pages. A folder with a `module.json` is a module: a configurable page whose per-item options live in `content_items.config`, with data fetched by `server/modules.py`.
- `deploy/` install and update scripts, systemd unit templates (`@REPO@`, `@USER@` placeholders).

## Rules

- Both programs run from the repo root: `python -m server`, `python -m agent`. Python 3.11+. In production the server runs under gunicorn (`wsgi.py`, one worker) and deploys with `scripts/update.sh`; `/healthz` needs no login.
- REST only between agent and server. No WebSocket, no MQTT. The agent polls. (The server alone may talk MQTT to Home Assistant: `server/ha_mqtt.py`.)
- Everything the agent shows is a URL in its own tab. Content is not iframed, with one exception Marc chose on 2026-10-04: split screens (`server/split.py`) put several content items in iframes on a grid on a server page, and the agent strips frame-blocking headers for those tabs.
- UI routes and the control API both go through `server/services.py`. Add operations there, not in the route.
- Changing the poll request or response means updating `shared/API.md` and, if old peers would break, bumping `PROTOCOL_VERSION`.
- Nothing may assume two displays, portrait, or 1080 x 2560.
- Runtime data, config with secrets and Chromium profiles stay out of the tracked tree.
- v2 items in DESIGN.md section 6 are not to be built yet, but must not be blocked.
- Anything marked "Verify on device" gets its findings written to `docs/on-device-verification.md`.

## Tests

`python -m pytest` from the repo root (install `requirements-dev.txt`). Add a test with every server or agent-logic change; `tests/conftest.py` has a `server` fixture with shortcuts for polling as an agent and posting forms. Schema changes need a migration in `server/db.py` and a test that upgrades an old database.

## Testing without a Pi

The agent runs against desktop Chrome with `detect_outputs = false`, `placement = "none"`, per-output `width`/`height` and `extra_chromium_args = ["--headless=new"]`. See the Development section of README.md.
