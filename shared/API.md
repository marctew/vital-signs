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
- `width` and `height` are the logical size after rotation and scaling.

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
- `refresh` is seconds between reloads, `0` for never. `zoom` is multiplied by `display_zoom`.
- `power` is `null` for always on. Otherwise it lists on-periods: the screen is powered from `on` to `off` on the listed days (Monday is 0) of any period, in the agent's local time, and off the rest of the time. An `off` earlier than `on` runs past midnight. An active override keeps the screen on. The agent reports `screen_on` per display.
- `power_override` is `null` or `{"id": 7, "on": false}`: force the screen on or off until the schedule next changes state. The agent ends it at that change and reports `power_override_done: <id>` for the display, which clears it on the server. A forced state wins over everything else, including waking for a URL override.
- `override` is `null` when none is active. `remaining_s` is relative so clocks need not agree. The agent also enforces the deadline itself.
- `commands` are redelivered on every poll until acknowledged; the agent ignores ids it has already run. `connector: null` targets every display. Types: `identify` (`args.seconds`, default 5), `reload`, `screenshot`, `vnc` (point wayvnc at this display), `next` and `previous` (step through the playlist; ignored during an override), `update` (run `deploy/update.sh`; the agent restarts if the pull brought changes). Agents ignore types they do not know.

### `POST /api/agent/screenshot?hostname=<hostname>&connector=<connector>`

Body is a JPEG (`Content-Type: image/jpeg`, at most 5 MB). Sent periodically and shortly after the content changes.

## Control API

For Home Assistant, n8n and the admin UI. If `control_token` is set in the server config, requests need `Authorization: Bearer <control_token>`; otherwise the API is open.

`<display>` is either the numeric display id or `<hostname>:<name>`, for example `signage-pi:left`.

| Method | Path | Body | Effect |
|---|---|---|---|
| GET | `/api/v1/displays` | | List displays with status |
| GET | `/api/v1/displays/<display>` | | One display |
| GET | `/api/v1/playlists` | | List playlists |
| PUT | `/api/v1/displays/<display>/assignment` | `{"playlist_id": 3}` or `{"playlist": "Name"}`; `null` unassigns | Assign a playlist |
| POST | `/api/v1/displays/<display>/override` | `{"url": "https://…", "minutes": 5}` | Push an override |
| DELETE | `/api/v1/displays/<display>/override` | | Clear the override |
| GET | `/api/v1/schedules` | | List screen power schedules |
| PUT | `/api/v1/displays/<display>/power-schedules` | `{"schedule_ids": [1, 2]}` or `{"schedules": ["Mornings"]}`; empty for always on | Assign screen power schedules |
| POST | `/api/v1/displays/<display>/power` | `{"on": true}` or `{"on": false}` | Force the screen on or off until its schedule next changes |
| DELETE | `/api/v1/displays/<display>/power` | | Go back to the schedule now |
| POST | `/api/v1/displays/<display>/identify` | optional `{"seconds": 5}` | Show the display's name |
| POST | `/api/v1/displays/<display>/reload` | | Reload all of the display's pages |
| POST | `/api/v1/displays/<display>/next` | | Skip to the next playlist item |
| POST | `/api/v1/displays/<display>/previous` | | Go back to the previous playlist item |
| POST | `/api/v1/displays/<display>/vnc` | | Make this display the one VNC shows |

Errors are `{"error": "message"}` with a 4xx status.

## Data proxy

`GET /api/data/<source>` returns the upstream response of a source named in the server config. Only query parameters listed in that source's `allow_params` are forwarded.
