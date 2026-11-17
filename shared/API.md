# Vital Signs API contract

Protocol version: **1** (`shared/protocol.py`).

All bodies are JSON unless stated. Times are Unix seconds.

## Agent API

Every agent request carries `Authorization: Bearer <agent_token>`.

If the database stays locked for 4 s (a stalled disk, such as during the nightly Proxmox backup), the server answers `503` with `Retry-After: 2` and `{"error": "database busy, try again"}`, and changes nothing. The agent treats it like any failed poll: it keeps showing what it has and tries again at its next poll.

### `POST /api/agent/poll`

Sent roughly every 2 seconds. Registers the agent and its displays on first sight.

Request:

```json
{
  "hostname": "signage-pi",
  "git_sha": "abc1234",
  "protocol_version": 1,
  "revision": "9f2c…",
  "acks": [12, 13],
  "health": {"temperature_c": 52.3, "memory_used_percent": 31, "memory_total_mb": 8062, "disk_used_percent": 18,
             "load": 0.4, "uptime_s": 86400, "network": "wifi", "interface": "wlan0", "wifi_signal_dbm": -55,
             "under_voltage": false, "throttled": false, "under_voltage_seen": false, "throttled_seen": false},
  "displays": [
    {
      "connector": "HDMI-A-1",
      "name": "left",
      "width": 1080,
      "height": 2560,
      "orientation": "portrait",
      "browser": "ok",
      "placement_ok": true,
      "override_active": false,
      "current": {"item_id": 4, "name": "Clock", "url": "http://…"},
      "errors": [{"item_id": 5, "name": "Grafana", "url": "http://…", "error": "HTTP 502"}]
    }
  ]
}
```

- `revision` is the revision of the desired state the agent last applied, or `null` (always `null` on the first poll after the agent starts).
- `acks` lists command ids the agent has executed. The server deletes them.
- `health` describes the Pi itself. Every field is optional; the agent leaves out what it cannot read.
- `width` and `height` are the logical size after rotation and scaling.
- `browser_memory_mb` is how much memory that display's Chromium is using, when the agent can tell.

Response:

```json
{
  "protocol_version": 1,
  "server_sha": "abc1234",
  "revision": "9f2c…",
  "poll_interval": 2,
  "screenshot_interval": 30,
  "commands": [{"id": 14, "type": "identify", "connector": "HDMI-A-1", "args": {}}],
  "displays": {
    "HDMI-A-1": {
      "display_zoom": 1.0,
      "power": [{"on": "06:30", "off": "08:30", "days": [0, 1, 2, 3, 4]},
                {"on": "17:30", "off": "23:00", "days": [0, 1, 2, 3, 4]}],
      "items": [
        {"id": 4, "content_id": 2, "name": "Clock", "url": "/pages/clock/", "local": true,
         "duration": 30, "zoom": 1.0, "refresh": 0, "css": ""}
      ],
      "override": {"id": 3, "url": "https://example.com", "local": false, "remaining_s": 287.5}
    }
  }
}
```

- `displays` is omitted when the request's `revision` equals the current one.
- `local: true` means the URL is a path on the server. The agent prefixes its `server_url` and appends `display`, `width`, `height` and `orientation` query parameters.
- A `local` URL starting `/rotate/` is a rotation: a server page that cycles through a playlist's items in iframes, normally placed in a split screen's cell. The agent treats it like a split.
- A `local` URL starting `/split/` is a split screen: a server page with other content in iframes. For tabs showing one, the agent removes `X-Frame-Options` and CSP `frame-ancestors` from document responses so framed sites load.
- `transition` is how the display moves between items: `none`, `fade` or `slow` (a fade through black; see `TRANSITIONS` in `shared/protocol.py`). A refresh of the page on screen never fades.
- `refresh` is seconds between reloads, `0` for never. `zoom` is multiplied by `display_zoom`.
- `scheduled` lists playlists that replace `items` while their period is active: `{"on", "off", "days", "playlist", "items"}`, with the same period rules as `power` and `items` in the same shape as the top-level list. The first active entry wins. The agent switches on its own clock.
- `power` is `null` for always on. Otherwise it lists on-periods: the screen is powered from `on` to `off` on the listed days (Monday is 0) of any period, in the agent's local time, and off the rest of the time. An `off` earlier than `on` runs past midnight. An active override keeps the screen on. The agent reports `screen_on` per display.
- `power_override` is `null` or `{"id": 7, "on": false}`: force the screen on or off until the schedule next changes state. The agent ends it at that change and reports `power_override_done: <id>` for the display, which clears it on the server. A forced state wins over everything else, including waking for a URL override.
- `override` is `null` when none is active. `remaining_s` is relative so clocks need not agree. The agent also enforces the deadline itself. When the override is a content item it also has that item's `zoom` and `css`, which the agent applies (an agent from before this was added ignores them and shows the page plain).
- `commands` are redelivered on every poll until acknowledged; the agent ignores ids it has already run. `connector: null` targets every display. Types: `identify` (`args.seconds`, default 5), `reload`, `screenshot`, `vnc` (point wayvnc at this display), `next` and `previous` (step through the playlist; ignored during an override), `update` (run `deploy/update.sh`; the agent restarts if the pull brought changes), `restart_browser` (relaunch Chromium), `reboot` (restart the Pi). `update` and `reboot` run only after their ack has been delivered. Agents ignore types they do not know.

### `POST /api/agent/screenshot?hostname=<hostname>&connector=<connector>`

Body is a JPEG (`Content-Type: image/jpeg`, at most 5 MB). Sent periodically and shortly after the content changes. Optional `content=<content id>` names the content item the picture shows (a loaded playlist item, not an override); the server keeps it as that item's preview.

## Device API (TRMNL e-ink)

What a TRMNL e-ink device calls. The paths are fixed by its firmware (`usetrmnl/firmware`), so they are at the site root. A device identifies itself with `ID: <MAC address>` and, once it has one, `Access-Token: <its key>`. These calls need no admin login. Code: `server/device_api.py`, `server/eink.py`. Design: `docs/specs/trmnl-eink.md`.

A device is **pending** from the first time it is seen until someone presses Adopt on the dashboard. A pending device gets only a picture telling it to be adopted.

### `GET /api/setup`

Headers: `ID`, `Model`, `FW-Version`, `Width`, `Height`, `RSSI`.

```json
{"status": 200, "api_key": "…", "friendly_id": "00180C", "image_url": null, "message": "Registered with Vital Signs"}
```

- An unknown MAC is remembered as pending and given a new key. Asking again returns the same key.
- Once the device is adopted the key is not handed out again: the answer is `404` with `"api_key": null`. **Re-pair** on the display's page opens it for one call in the next 10 minutes, for a device whose settings were wiped.

### `GET /api/display`

Sent on every wake. Headers, as the firmware builds them: `ID`, `Access-Token`, `Battery-Voltage`, `RSSI`, `FW-Version`, `Model`, `Width`, `Height`, `Update-Source` (why it woke), `Refresh-Rate`, `Reset-Reason`, `Wake-Time`, and on boards that can tell, `USB-Connected` (`true` / `false`) and `Battery-Charging` (`1` / `0`).

```json
{
  "status": 0,
  "image_url": "http://vitalsigns.lan:8080/api/eink/image/7/3f9c2a1b5d6e7f80.png",
  "filename": "3f9c2a1b5d6e7f80",
  "refresh_rate": 900,
  "update_firmware": false, "firmware_url": "", "reset_firmware": false,
  "special_function": "sleep"
}
```

- `filename` is the first 16 hex digits of the SHA-256 of the PNG, so the same picture always has the same name and the firmware does not fetch it twice.
- `image_url` uses the host the request came to, and `X-Forwarded-Proto` when a reverse proxy sets it.
- `refresh_rate` is how many seconds to sleep: until the next thing that could change the picture, kept between the display's shortest and longest sleep. It is 20 while a picture is being drawn, and 60 for a pending device.
- A wrong key is `401`. A missing `ID` is `400`. The body's `status` is never anything but `0`: the firmware treats `500` as an order to wipe itself.
- A MAC that is not known but arrives with a key (a device set up against another server, or forgotten here) is remembered as pending with that key, so adopting it needs nothing done on the device.
- The server never offers a firmware update.

### `GET /api/eink/image/<folder>/<filename>.png`

The picture a display reply named: an indexed PNG at the panel's size, 2 bits per pixel for a four-colour panel (black, white, red, yellow) and 1 bit for black and white. Needs the same `ID` and `Access-Token`; a device can only fetch its own.

### `POST /api/log`

Body `{"logs": [ … ]}`, with `ID` and `Access-Token`. The last 200 entries per device are kept and the newest shown on the display's page. Answers `204`.

## Control API

For Home Assistant, n8n and the admin UI. Requests need `Authorization: Bearer <control_token>` (set `control_token` in the server config) or a logged-in admin session. The API is open only when neither a control token nor an admin password is configured.

`<display>` is either the numeric display id or `<hostname>:<name>`, for example `signage-pi:left`.

| Method | Path | Body | Effect |
|---|---|---|---|
| GET | `/api/v1/displays` | | List displays with status |
| GET | `/api/v1/displays/<display>` | | One display |
| GET | `/api/v1/playlists` | | List playlists |
| POST | `/api/v1/boards/<name or id>` | see below | Set what a Board module shows |
| GET | `/api/v1/agents` | | List the Pis with their health readings |
| POST | `/api/v1/agents/<hostname or id>/restart-browser` | | Relaunch Chromium on every display of that Pi |
| POST | `/api/v1/agents/<hostname or id>/reboot` | | Reboot that Pi |
| PUT | `/api/v1/displays/<display>/assignment` | `{"playlist_id": 3}` or `{"playlist": "Name"}`; `null` unassigns | Assign a playlist |
| GET | `/api/v1/content` | | List content items (id, name, kind, url) |
| POST | `/api/v1/displays/<display>/override` | `{"url": "https://…", "minutes": 5}`, or `{"content": "Meal plan"}` or `{"content_id": 12}` in place of `url` | Push an override: an address, or a content item by name (case does not matter) or id, shown with its own zoom, CSS and settings |
| DELETE | `/api/v1/displays/<display>/override` | | Clear the override |
| GET | `/api/v1/schedules` | | List screen power schedules |
| PUT | `/api/v1/displays/<display>/power-schedules` | `{"schedule_ids": [1, 2]}` or `{"schedules": ["Mornings"]}`; empty for always on | Assign screen power schedules |
| POST | `/api/v1/displays/<display>/playlist-rules` | `{"schedule": "Mornings", "playlist": "News"}` (or `schedule_id`, `playlist_id`) | Show a playlist while a schedule is active |
| DELETE | `/api/v1/displays/<display>/playlist-rules/<id>` | | Remove that rule |
| POST | `/api/v1/displays/<display>/power` | `{"on": true}` or `{"on": false}` | Force the screen on or off until its schedule next changes |
| DELETE | `/api/v1/displays/<display>/power` | | Go back to the schedule now |
| POST | `/api/v1/displays/<display>/refresh` | | E-ink only: draw its pictures again now |
| GET | `/api/v1/displays/<display>/battery?days=7` | | E-ink only: battery readings and a battery life estimate (below) |
| POST | `/api/v1/displays/<display>/identify` | optional `{"seconds": 5}` | Show the display's name |
| POST | `/api/v1/displays/<display>/reload` | | Reload all of the display's pages |
| POST | `/api/v1/displays/<display>/next` | | Skip to the next playlist item |
| POST | `/api/v1/displays/<display>/previous` | | Go back to the previous playlist item |
| POST | `/api/v1/displays/<display>/vnc` | | Make this display the one VNC shows |

Errors are `{"error": "message"}` with a 4xx status.

A display has `"kind": "screen"` (on a Pi) or `"eink"`. An e-ink display also has an `eink` object: `friendly_id`, `model`, `model_name`, `palette`, `firmware`, `battery_v`, `battery_percent` (null on mains), `rssi`, `wifi` (`good`, `OK` or `weak`), `usb` (null when the board cannot tell), `wake_reason`, `last_seen`, `next_expected_at`, `refresh_s`, `mains`, `min_interval_s`, `max_interval_s`, `heartbeat_s`, `footer`, `insets` (pixels hidden behind the frame: `top`, `right`, `bottom`, `left`), `min_text_px` (smallest text drawn, 0 for no limit), `drawn_at`, `checked_at`, `stale`, `awake`, `pairing` and `renderer`. Its `online` means it made the last wake it was told to make. The screen power, identify, reload, next, previous and vnc calls do nothing useful for one. An agent in `/api/v1/agents` has `"kind": "pi"` or `"eink"`.

### Battery history

`GET /api/v1/displays/<display>/battery?days=<1 to 400>` (default 7), for an e-ink display:

```json
{
  "days": 7, "from": 1794300000, "to": 1794904800, "mains": false, "count": 912, "first_reading": 1793885494,
  "samples": [{"at": 1794301260, "volts": 3.874, "percent": 79}],
  "estimate": {"since": 1793885494, "hours": 283.0, "per_day": 0.92, "days_left": 98.4, "empty_at": 1803406000, "full_charge_days": 108.7}
}
```

- A reading is kept each time the device wakes (at most one every two minutes), for 400 days. `samples` is those readings averaged into at most 240 points across the range; `count` is how many readings they came from, and `first_reading` when the device's history starts.
- `percent` is worked out from `volts` when asked for, so it is a guide: 4.1 V and above is 100, 3.0 V is 0.
- `estimate` is a straight line through the current discharge, which is everything since the level last rose by 5 points or more. It is `null` until 24 hours of that are on record. `days_left`, `empty_at` and `full_charge_days` are `null` while the level has fallen less than 2 points, which is too little to tell from noise.

### Boards

A Board module shows whatever was last sent to it. Every field is optional; each push replaces the last one.

```json
{
  "title": "Workshop",
  "value": 12, "label": "Open jobs", "status": "warn",
  "text": "Two overdue",
  "items": [{"label": "Smith, boiler service", "value": "09:30", "status": "ok"}, "Parts delivery due"]
}
```

`status` is `ok`, `warn` or `bad` and colours the figure or a row. A board with nothing sent, or with nothing sent for longer than its "stale" setting, counts as empty and can hide in a split screen.

## Data proxy

`GET /api/data/<source>` returns the upstream response of a source named in the server config. Only query parameters listed in that source's `allow_params` are forwarded.
