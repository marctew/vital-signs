# Vital Signs API contract

Protocol version: **1** (`shared/protocol.py`).

All bodies are JSON unless stated. Times are Unix seconds.

## Agent API

Every agent request carries `Authorization: Bearer <agent_token>`.

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
- `override` is `null` when none is active. `remaining_s` is relative so clocks need not agree. The agent also enforces the deadline itself.
- `commands` are redelivered on every poll until acknowledged; the agent ignores ids it has already run. `connector: null` targets every display. Types: `identify` (`args.seconds`, default 5), `reload`, `screenshot`, `vnc` (point wayvnc at this display), `next` and `previous` (step through the playlist; ignored during an override), `update` (run `deploy/update.sh`; the agent restarts if the pull brought changes), `restart_browser` (relaunch Chromium), `reboot` (restart the Pi). `update` and `reboot` run only after their ack has been delivered. Agents ignore types they do not know.

### `POST /api/agent/screenshot?hostname=<hostname>&connector=<connector>`

Body is a JPEG (`Content-Type: image/jpeg`, at most 5 MB). Sent periodically and shortly after the content changes. Optional `content=<content id>` names the content item the picture shows (a loaded playlist item, not an override); the server keeps it as that item's preview.

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
| POST | `/api/v1/displays/<display>/override` | `{"url": "https://…", "minutes": 5}` | Push an override |
| DELETE | `/api/v1/displays/<display>/override` | | Clear the override |
| GET | `/api/v1/schedules` | | List screen power schedules |
| PUT | `/api/v1/displays/<display>/power-schedules` | `{"schedule_ids": [1, 2]}` or `{"schedules": ["Mornings"]}`; empty for always on | Assign screen power schedules |
| POST | `/api/v1/displays/<display>/playlist-rules` | `{"schedule": "Mornings", "playlist": "News"}` (or `schedule_id`, `playlist_id`) | Show a playlist while a schedule is active |
| DELETE | `/api/v1/displays/<display>/playlist-rules/<id>` | | Remove that rule |
| POST | `/api/v1/displays/<display>/power` | `{"on": true}` or `{"on": false}` | Force the screen on or off until its schedule next changes |
| DELETE | `/api/v1/displays/<display>/power` | | Go back to the schedule now |
| POST | `/api/v1/displays/<display>/identify` | optional `{"seconds": 5}` | Show the display's name |
| POST | `/api/v1/displays/<display>/reload` | | Reload all of the display's pages |
| POST | `/api/v1/displays/<display>/next` | | Skip to the next playlist item |
| POST | `/api/v1/displays/<display>/previous` | | Go back to the previous playlist item |
| POST | `/api/v1/displays/<display>/vnc` | | Make this display the one VNC shows |

Errors are `{"error": "message"}` with a 4xx status.

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
