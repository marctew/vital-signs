# Spec: TRMNL e-ink displays

Make a TRMNL e-ink device a display in Vital Signs: it shows playlists and overrides like a Pi screen, reports its battery and signal to Home Assistant, says on the panel how old its data is, and can be told to show a given page on demand ("Alexa, show me the meal plan").

Read `CLAUDE.md` and `DESIGN.md` first. This spec adds a second kind of display and a new API surface for it. It changes nothing in the Pi agent or the agent protocol.

## Changed during the build

Phase 1 was built on 2026-11-17. It differs from the spec below in these places. The code is the reference.

- **Adoption.** The spec had `/api/setup` answer `404` until the device was adopted. Instead a new device gets its key at once and is *pending*: `/api/display` sends it a picture saying "Adopt this display" with its ID, so the panel shows what to do and the ID to match. A pending device has no agent or display row. Once adopted, `/api/setup` stops giving the key out (knowing a MAC must not be enough to get it); **Re-pair** on the display's page opens it for one call. A device that turns up at `/api/display` already holding a key is taken as pending with that key.
- **No "Next ~HH:MM" on the footer.** The picture is drawn before the wake that fetches it, and the time of the wake after that is only worked out during it. The next check-in is on the dashboard and in Home Assistant instead.
- **The footer is drawn with Pillow, not as a page**, with anti-aliasing off. It is crisper on the panel, needs no browser, and its text is tested.
- **The built-in pictures** (adopt me, no playlist, asleep, could not draw) are also Pillow, so they work with no Chromium installed.
- **Playlist rotation is stateless**: the current item is worked out from the clock (`now` modulo the playlist's length), so `rotation_index` and `rotation_since` were not needed.
- **`eink_renders` table.** The spec kept only files. A row per picture holds what the wake-interval and unchanged-picture rules need: when it was checked, when it was published, a hash of the body, the last error.
- **"What the panel shows" is what was last offered**, recorded on `/api/display`, not on the image fetch: the firmware skips the fetch for a picture it already holds.
- **`eink_refresh` per content item** ("Check for new data every N minutes"). Modules have no refresh interval of their own, so without it every module would be redrawn only at the display's longest sleep.
- **A pushed Board is redrawn at once**, as is an item whose settings are saved.
- **Pages do not have to call `VS.ready()`.** `vitalsigns.js` calls it once `VS.module()`, `VS.moduleData()` and `VS.data()` have settled and images have loaded, so every existing module works unchanged.
- **`eink_logs` hangs off the device**, not the agent, so a pending device's errors are kept too.
- **`POST /api/v1/displays/<display>/refresh`** was added (the HA "Draw again" button's API twin).
- **The alert blueprint** gained a battery level trigger; Online and Data stale already fit its existing inputs.
- **Checked on e-ink so far:** Meal plan, Weather, Board and Notice were drawn through real Chromium at 800 x 452 and looked at. Calendar and Home Assistant sensors are marked for e-ink and use the shared styles, but were not drawn with live data.
- **HomeForm is the Flask rewrite, not the version this spec was written against.** The live HomeForm (branch `flask`, port 8797) has no `/api/public/meals`; it answers meals, bins and shopping as feeds of its standard API (`/api/v1/feeds/<name>`) behind a read token. The Meal plan module was changed to use the meals feed with a token option (up to 7 days), and a Bins module was added from the bins feed, so phase 3's "Bins needs a HomeForm route" no longer applies.
- **First wake after setup (seen on the device, firmware 1.8.19):** after `/api/setup` the panel shows the firmware's own "Please visit trmnl.com/start" screen and did not call `/api/display` in the same wake. It fetched its first picture after a power-cycle.
- **Frame margins** (added after the panel went on the wall: its frame covers a few millimetres of each edge). A display has four insets in pixels. The page is rendered at the smaller size and pasted, with the footer, inside them; the covered edge stays white. The picture sent is still the full panel size.
- **Not done:** hiding "not designed for e-ink" modules is only a tooltip on the Add a module buttons.

## Why

Marc's TRMNL BWRY (7.5", 800 x 480, black / white / red / yellow) runs on TRMNL's cloud. Three things are wrong with that:

1. It is not clear when the data on the panel was last refreshed.
2. Low battery and "gone offline" alerts are unreliable.
3. There is no easy way to put a chosen view on the panel now: an alert, or a page asked for by voice.

TRMNL's firmware supports pointing the device at another server ("BYOS"). Vital Signs already has displays, playlists, schedules, overrides, modules, a control API and Home Assistant entities, so it becomes that server.

## What was checked, and against what

Read from the firmware source, `usetrmnl/firmware` at commit `d5b610b` (2026-11-16). Nothing here has been tried on the device yet.

- The device calls three endpoints on its server's root: `GET /api/setup`, `GET /api/display`, `POST /api/log`. It then downloads the image the display reply names, draws it and deep-sleeps for `refresh_rate` seconds.
- There are two BWRY hardware generations, told apart by the `Model` header. Marc's is gen 1 (confirmed with `esptool` on 2026-11-17):

| Model | Build env | Chip | Battery reading | Knows USB is plugged in |
|---|---|---|---|---|
| `og_4clr` | `trmnl_4clr` | ESP32-C3 | voltage (ADC) | no |
| `og_gen2_4clr` | `trmnl_gen2_4clr` | ESP32-C5 | BQ27427 gauge | yes (`USB-Connected`, `Battery-Charging` headers) |

- Gen 1 is an ESP32-C3 (2.4 GHz Wi-Fi only); gen 2 is an ESP32-C5 (dual-band Wi-Fi, PSRAM). Both drive the same 800 x 480 four-colour panel. A four-colour image is always a full panel refresh; there is no fast or partial refresh.
- The firmware accepts PNG (1, 2, 4, 8-bit indexed, or truecolour), JPEG and BMP. For BWRY it maps every pixel to the nearest of the four inks itself (`GetBWYRPixel`), so pure `#000000`, `#FFFFFF`, `#FF0000` and `#FFFF00` come through exactly.
- When the reply's `filename` matches an image already stored in flash, the firmware does not download it again.

TRMNL's own BYOS guide (`docs.trmnl.com/go/diy/byos`) and help pages could not be read while writing this (blocked from the build session). Read the guide before starting and correct this spec where it differs.

## Decisions already made

- **It lives in Vital Signs**, not a separate app. A TRMNL is an agent with one display.
- **REST only**, as for the Pi. No MQTT or WebSocket to the device, including in the forked firmware.
- **The server renders.** A TRMNL cannot run a browser, so the server screenshots the page with headless Chromium and sends a PNG. Pages are the same pages the Pi shows, with an e-ink stylesheet.
- **Freshness is printed as clock times, never as "5 min ago".** A relative age goes stale on paper.
- **Stock firmware first, fork second.** Phase 1 uses TRMNL's firmware unchanged. Phase 2 forks it only to add a stay-awake mode for mains power.
- **Nothing may assume BWRY or 800 x 480.** Size comes from the `Width` / `Height` headers and the palette from a table keyed on `Model`, so a mono TRMNL OG works by adding a row.
- **Vital Signs never pushes firmware.** `update_firmware` is always `false`.

## Phases

1. **Server, stock firmware.** Device API, renderer, e-ink display kind, footer, Home Assistant entities, show-on-demand, meals module. Fixes problems 1 and 2 in full, and 3 with a delay of up to the wake interval.
2. **Mains power and firmware fork.** Stay-awake polling so an override reaches the panel in about 25 seconds. A separate repo and a small server change.
3. **More e-ink pages.** Bins (needs a HomeForm route) and the remaining modules.

Each phase is usable alone. Do them in order.

## Phase 1

### Files

| Path | What |
|---|---|
| `server/device_api.py` | Blueprint for `/api/setup`, `/api/display`, `/api/log` and `/api/eink/image/…`. |
| `server/eink.py` | Renderer thread, palette table, quantiser, footer, image store, wake-interval logic. |
| `server/services.py` | E-ink operations (adopt, settings, pick the current item, record a wake). `_display_dict` gains the e-ink fields. `push_override` accepts a content item. |
| `server/db.py` | New tables and columns (below) and the migration. |
| `server/ha_mqtt.py` | Entities for e-ink displays; skip the ones that mean nothing for them. |
| `server/control_api.py` | `content` on the override body; `GET /api/v1/content`. |
| `server/templates/display.html`, `_display.html`, `dashboard.html` | E-ink variant of the display card and page. |
| `pages/_shared/eink.css`, `pages/_shared/vitalsigns.js` | E-ink look, `VS.medium`, `VS.ready()`. |
| `pages/meals/` | New module: `module.json`, `index.html`. |
| `server/modules.py` | `fetch_meals` provider. |
| `deploy/install-server.sh`, `server/requirements.txt` | Playwright and its Chromium. |
| `shared/API.md` | New "Device API (TRMNL)" section and the control API additions. |
| `homeassistant/blueprints/vitalsigns_problem_alert.yaml` | Low battery and overdue as things it can watch. |
| `tests/test_device_api.py`, `tests/test_eink.py`, `tests/test_meals_module.py` | Tests (below). |
| `README.md`, `DESIGN.md` §9, `docs/on-device-verification.md` | Docs and findings. |

### Data model

```sql
ALTER TABLE displays ADD COLUMN kind TEXT NOT NULL DEFAULT 'screen';   -- 'screen' or 'eink'

CREATE TABLE eink_devices (
    agent_id INTEGER PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    mac TEXT NOT NULL UNIQUE,
    api_key TEXT NOT NULL,
    friendly_id TEXT NOT NULL,
    adopted INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    fw_version TEXT NOT NULL DEFAULT '',
    telemetry TEXT NOT NULL DEFAULT '{}',      -- last wake: battery_v, rssi, usb, charging, wake_reason, reset_reason
    refresh_s INTEGER NOT NULL DEFAULT 0,      -- the sleep we last told it
    next_expected_at REAL NOT NULL DEFAULT 0,
    mains INTEGER NOT NULL DEFAULT 0,          -- "this device is on permanent power"
    min_interval_s INTEGER NOT NULL DEFAULT 300,
    max_interval_s INTEGER NOT NULL DEFAULT 3600,
    heartbeat_s INTEGER NOT NULL DEFAULT 3600, -- redraw at least this often so the footer time moves
    drawn_filename TEXT NOT NULL DEFAULT '',   -- what the panel shows now, as far as we know
    drawn_at REAL NOT NULL DEFAULT 0,
    drawn_content_id INTEGER,
    rotation_index INTEGER NOT NULL DEFAULT 0,
    rotation_since REAL NOT NULL DEFAULT 0
);

CREATE TABLE eink_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    at REAL NOT NULL,
    body TEXT NOT NULL
);
```

- The agent row's `hostname` is `trmnl-<friendly_id lowercased>`, renameable later. The display row's `connector` is `eink` and its `name` defaults to `panel`, so it is addressed as `trmnl-ab12cd:panel` in the control API like any other display.
- `agents.last_seen` is set on every `/api/display` call.
- Keep the last 200 `eink_logs` rows per device.
- Rendered images are files under `<data>/eink/<display id>/`, not database rows. They are not backed up.

### Device API

Served from the site root because the firmware's paths are fixed. They do not clash with `/api/agent`, `/api/v1` or `/api/data`. They need no admin login.

**`GET /api/setup`** — headers `ID` (MAC), `Model`, `FW-Version`, `Width`, `Height`, `RSSI`.

- Unknown MAC: create the agent, display and `eink_devices` row with `adopted = 0`, a random `api_key` and a 6-character `friendly_id`. Answer `404` with `{"status": 404, "message": "Adopt this display in Vital Signs"}`. The firmware shows a "not registered" screen and tries again later.
- Known but not adopted: `404` as above.
- Adopted: `200` with `{"status": 200, "api_key": "…", "friendly_id": "AB12CD", "image_url": "<server>/api/eink/setup.png", "message": "Registered with Vital Signs"}`.

Adoption is one button on the dashboard ("New e-ink display AB12CD · Adopt"). This is the only gate: the setup call itself carries no secret, so an unadopted device must get nothing.

**`GET /api/display`** — headers as built by the firmware's `buildDisplayHeaders`: `ID`, `Access-Token`, `Battery-Voltage`, `RSSI`, `FW-Version`, `Model`, `Width`, `Height`, `Update-Source` (why it woke: timer, button, …), `Refresh-Rate`, `Reset-Reason`, `Wake-Time`, `Image-Cached`, and on gen 2 `USB-Connected` and `Battery-Charging`.

- `ID` + `Access-Token` must match an adopted device; otherwise answer HTTP `401`. Never send a body `status` of `500`: the firmware treats it as an order to wipe its credentials.
- Record the telemetry, `last_seen`, size and model.
- Work out what should be showing (below), and reply:

```json
{
  "status": 0,
  "image_url": "http://vitalsigns.lan:8080/api/eink/image/7/3f9c2a1b.png",
  "filename": "3f9c2a1b",
  "refresh_rate": 900,
  "update_firmware": false,
  "firmware_url": "",
  "reset_firmware": false,
  "special_function": "sleep"
}
```

- `filename` is the first 16 hex characters of the SHA-256 of the PNG bytes. Same picture, same name, so the firmware does not fetch it again.
- `image_url` is built from the request's host, so it works through Zoraxy and by IP.
- Store `refresh_s` and `next_expected_at = now + refresh_s + 30` (the 30 covers a typical wake).
- If nothing has ever been rendered for the device, send a built-in "Waiting for first picture" PNG and `refresh_rate: 30`.
- The handler must answer in well under a second. It never renders; it hands out the newest finished image.

**`GET /api/eink/image/<display id>/<filename>.png`** — needs the same `ID` and `Access-Token` headers (the firmware sends them). Serving it sets `drawn_filename`, `drawn_at` and `drawn_content_id`: this is our evidence of what the panel shows.

**`POST /api/log`** — body `{"logs": [ … ]}`. Store each entry in `eink_logs`. Answer `204`. Show the last few on the display's page.

### What should be showing

One function in `services.py`, used by the device API, the dashboard and Home Assistant: `eink_current(db, display, now)` returns the content item and the time it stops being current.

Priority, the same as the Pi's:

1. An active override.
2. The playlist of the first active playlist rule (playlists by time).
3. The assigned playlist.
4. Nothing: a built-in "No playlist assigned" picture.

A playlist rotates on the server's clock: `rotation_index` advances when `now - rotation_since` reaches the item's duration. Durations are in seconds as now; on e-ink anything under the device's minimum interval is treated as that minimum, and the playlist editor says so for a playlist assigned to an e-ink display.

**Screen power schedules** mean "do not wake" for e-ink: outside the on-periods the panel keeps its last picture and `refresh_rate` is the time until the next on-period (capped at 12 hours so it still checks in). An active override ignores this, as on the Pi. The manual screen on/off switch is hidden for e-ink.

### The wake interval

`refresh_rate` is the time until the next thing that could change the picture, clamped to the device's limits:

```
next_change = min(end of the current item's turn,
                  the current item's next re-render (its refresh interval),
                  override expiry,
                  next playlist rule or power schedule boundary,
                  heartbeat due)
refresh_rate = clamp(next_change - now, min_interval_s, max_interval_s)
```

- If a render for the current item is still running, `refresh_rate` is 20 so the device comes straight back for it.
- A content item's `refresh_interval` of 0 means "never reload" on a Pi. On e-ink 0 means "use the device's maximum interval".
- Defaults: 5 minutes minimum, 60 maximum. With `mains` ticked the minimum drops to 60 seconds. All three are on the display's page.

### Renderer (`server/eink.py`)

One background thread, started with the app (gunicorn runs one worker, so one thread). It owns a single headless Chromium via Playwright's sync API and keeps it open.

**Why Playwright and not the system Chromium:** on Ubuntu the `chromium` package is a snap, which does not run cleanly in an LXC. Playwright downloads its own build. `install-server.sh` runs `playwright install --with-deps chromium`; say so in the README because it adds about 500 MB. If the import or launch fails, the server still starts, e-ink displays show "Renderer unavailable" on the dashboard, and `/healthz` stays green.

**What gets rendered, and when**

- The current item of every adopted e-ink display, re-rendered when its refresh interval is due or its settings change.
- The next item in the playlist, one minimum-interval ahead of its turn, so a rotation never waits for a render.
- An override, immediately when it is pushed.
- Content items ticked "Keep ready for e-ink" (new checkbox on the content form): re-rendered on their refresh interval for every e-ink display, so showing one on demand is instant.

**How**

1. Viewport: the device's width by its height minus the footer (28 px). Device scale factor 1.
2. URL: what the Pi agent would load (server URL prefixed for local pages, plus `display`, `width`, `height`, `orientation`), plus `medium=eink&palette=bwry`. Apply the item's zoom and injected CSS as the agent does.
3. Wait for the page to say it is done: `window.__vsReady === true`, set by `VS.ready()`. Pages that never call it (external URLs) are taken 2 seconds after the load event. Give up at 20 seconds and keep the previous image; record the failure against the item so the dashboard flags it like a Pi page error.
4. Screenshot to PNG.
5. Render the footer (below) as its own tiny page, 28 px high, and paste it under the screenshot with Pillow.
6. Quantise to the palette with Pillow and save as an indexed PNG: 2-bit for four colours, 1-bit for mono.
7. Name it by content hash. Keep the newest 20 files per display.

**Palettes**

```python
PALETTES = {
    "bwry": [(0, 0, 0), (255, 255, 255), (255, 0, 0), (255, 255, 0)],
    "bw":   [(0, 0, 0), (255, 255, 255)],
}
MODELS = {"og_4clr": "bwry", "og_gen2_4clr": "bwry", "og": "bw", "og_gen2": "bw"}   # unknown model: "bw"
```

**Dithering** is per content item, a new select on the content form, shown only when an e-ink display exists:

- `none` (default for modules): each pixel goes to the nearest ink. Right for pages drawn in flat colour; text stays crisp.
- `dither` (default for URLs, Image and Photo frame): Floyd-Steinberg. Right for photos and sites not made for e-ink.

**Unchanged pictures.** The footer carries a time, so two renders of unchanged data would differ only there and cost a 20-second flashing refresh for nothing. So: hash the picture without the footer. If that hash equals the one on the panel and the heartbeat is not due, keep serving the image already drawn. When the heartbeat is due (default hourly), publish the new one, footer and all.

### Footer

A 28 px strip on every e-ink picture, black text on white with a hairline above. Left to right:

- **Checked 14:32** — when the server last rendered this picture with fresh data. Red, and worded **Data from 09:15**, when the item's data is stale: its provider's last success is older than twice its refresh interval, or the last render failed.
- The item's name.
- **Next ~15:30** — when the device will next wake, rounded to the minute. Omitted in stay-awake mode.
- Battery percent and a Wi-Fi strength word (Good / OK / Weak), from the last wake. Battery in red under 20%. Omitted when on mains.

A module can add its own source times: `VS.sourceTime(label, unixSeconds)` adds "Meals 13:05" to the strip. Without that the strip uses the time the module's provider last succeeded (`modules.data` already caches per item; expose the timestamp).

The footer can be turned off per display (photo frames).

Times are the server's local time, 24-hour.

### E-ink pages

`vitalsigns.js` reads `medium` and `palette` from the query string. When `medium=eink` it sets `data-medium="eink"` and `data-palette` on `<html>` and loads `/pages/_shared/eink.css`, which:

- sets a white background and black text, and redefines the shared colour variables to the four inks (accent and "bad" are red, "warn" is yellow on a black outline because yellow text on white is unreadable);
- turns off every animation and transition;
- thickens hairlines to 2 px and drops shadows, gradients and opacity.

New API on `VS`:

- `VS.medium` — `"screen"` or `"eink"`.
- `VS.ready()` — "the page is fully drawn". Every module calls it once its data is on the page. On a Pi it does nothing.
- `VS.sourceTime(label, unixSeconds)` — see Footer.

A module that pages or scrolls (Home Assistant sensors, RSS) shows its first page only on e-ink; rotation comes from putting several items in the playlist.

`module.json` gains `"eink": true` for modules checked on the panel. The "Add a module" list and the playlist editor mark the rest "not designed for e-ink" when the playlist is on an e-ink display. They still work, dithered.

**Checked in phase 1:** Weather, Calendar, Board, Notice, Home Assistant sensors, and Meals (new). Each gets a look on the real panel and any fixes it needs; note the findings.

**Not worth doing:** Flights, Frigate, Plex now playing, Clock (the panel cannot keep time to the minute).

### Meals module

`pages/meals/` — the next days of the HomeForm meal plan.

- Data: `GET <homeform>/api/public/meals`, which needs no login and returns `{"dates": ["2026-11-17", …], "plan": {"2026-11-17": {"lunch": ["Soup"], "dinner": ["Chilli"]}}}` for today and the next four days.
- Provider `fetch_meals`, registered as `"meals": (fetch_meals, 300)`.
- Options: `title`; `url` (HomeForm address, required, e.g. `http://homeform.lan:3001`); `days` (1 to 5, default 5); `slots` (`both` or `dinner`).
- Layout: today large at the top with a red "Today" label, the following days as rows beneath. A day with nothing planned says "Nothing planned". Works on a Pi screen too, in the normal module style.
- `"can_hide": true`: nothing planned in the whole range counts as empty.
- `"eink": true`.

### Show on demand

Today an override is a URL. Add a content item:

- `POST /api/v1/displays/<display>/override` accepts `{"content": "Meal plan", "minutes": 10}` or `{"content_id": 12, "minutes": 10}` as well as `url`. Name matching is case-insensitive and exact. The override stores the item's id so its zoom, CSS, dithering and module settings apply.
- `overrides` gains `content_id INTEGER REFERENCES content_items(id) ON DELETE CASCADE`, nullable. A Pi display given a content override gets that item's URL and settings in the poll reply, so this works on every display, not only e-ink.
- `GET /api/v1/content` lists content items (id, name, kind), for building automations.
- The display page's override form gets a content picker next to the URL box.

How fast it appears, phase 1: the override is rendered at once (or is already rendered if "Keep ready" is ticked), and the device picks it up at its next wake. That is up to the minimum interval: 5 minutes on battery, 60 seconds on mains, plus about 25 seconds for the device to connect and the panel to refresh. Phase 2 removes the wait.

### Home Assistant

Each e-ink device appears as a device with:

| Entity | Type | Notes |
|---|---|---|
| Online | binary_sensor (connectivity) | Off when `now > next_expected_at + max(120, refresh_s)`: it missed a wake it was told to make. |
| Battery | sensor (battery, %) | From voltage: 4.1 V and above is 100, 3.0 V is 0, straight line between. Unavailable on mains. |
| Battery voltage | sensor (voltage, diagnostic) | |
| Wi-Fi signal | sensor (signal_strength, dBm, diagnostic) | |
| Last check-in | sensor (timestamp) | |
| Next check-in | sensor (timestamp) | |
| Showing | sensor | Name of the item on the panel (`drawn_content_id`). |
| Picture drawn | sensor (timestamp) | `drawn_at`. |
| Data stale | binary_sensor (problem) | The footer's stale rule. |
| On USB power | binary_sensor (plug, diagnostic) | Only when the device reports it (gen 2). |
| Firmware | sensor (diagnostic) | |
| Playlist | select | As for Pi displays. |
| Show | select | Content items; choosing one pushes a 10-minute override. Reads "Playlist" when no override is active; choosing "Playlist" clears it. |
| Refresh | button | Forces a re-render of the current item. |

Not created for e-ink: screen switch, Identify, Reload, Next, Previous, Restart browsers, Reboot, temperature, memory, power.

"Alexa, show me the meal plan" is then an HA script that sets the Show select (or calls the control API), exposed to Alexa. An alert is the same call from an automation. Document both in the README with a worked example.

Extend the problem-alert blueprint so it can watch Online and a battery threshold.

### Admin UI

Follow `APP-STANDARD.md` and `DESIGN-SPEC.md`; no one-off styles.

- **Dashboard card:** the picture is the last PNG served to the device, which is exactly what the panel shows. Under it: item name, battery, signal, "Checked in 14:32 · next 15:30". Overdue shows as offline in the usual way.
- **Display page:** playlist, playlists by time, schedules (labelled "Wake hours" for e-ink), override with content picker, and an e-ink settings card: mains power, minimum / maximum interval, heartbeat, footer on or off. Hide zoom, transition, screen power, Identify, Reload, Next / Previous and VNC. Show the last device logs.
- **Adopt** banner on the dashboard for unadopted devices, with Adopt and Forget.

### Tests

- Setup: unknown MAC creates an unadopted device and answers 404; adopted answers 200 with the key; the key never changes on a repeat call.
- Display: wrong token is 401; telemetry is stored; the reply names the newest finished image; `filename` is stable for identical bytes; no image yet gives the waiting picture and a 30-second rate.
- Wake interval: each term of `next_change` in turn, the clamps, the pending-render case, outside wake hours, mains minimum.
- Current item: override beats rule beats assignment; rotation advances on the clock; a content override carries the item's settings; expiry returns to the playlist.
- Quantiser: the four pure inks survive untouched; a mid-grey goes to black or white with `none` and to a mix with `dither`; output is an indexed PNG of the right bit depth and size.
- Unchanged picture: same body with a new footer time is not republished before the heartbeat, and is after it.
- Staleness: provider failure turns the footer wording and the HA problem sensor on.
- Online: overdue by more than the grace is offline.
- Home Assistant: the e-ink entity set, and that Pi-only entities are absent.
- Meals: parsing, `days`, `slots`, empty plan.
- Migration: an old database upgrades, and existing displays get `kind = 'screen'`.
- The renderer itself is not driven in tests (no browser in CI): put Chromium behind one function and fake it, as the agent tests do.

### Verify on device

Write the answers into `docs/on-device-verification.md`.

1. Which generation is it: what does the `Model` header say?
2. Does the stock firmware take a plain `http://` server address, or must it be `https://`? If https, does it accept the certificate Zoraxy serves? (From the source: `http://` is accepted. See `docs/on-device-verification.md`.)
3. When `/api/display` returns the `filename` already on the panel, does the panel stay untouched (no flash)? Phase 1's short mains interval depends on this. If it redraws every wake, raise the mains minimum to 5 minutes until phase 2.
4. How long is one full wake, and one four-colour refresh?
5. Do pure red and yellow come out clean, and is `dither` acceptable for photos?
6. What is the smallest `refresh_rate` the firmware honours?
7. Largest PNG it will take (the build sets `PNG_MAX_BUFFERED_PIXELS=6432`; a 2-bit 800 x 480 PNG should be well inside it).

## Phase 2: mains power and the firmware fork

### Power

Permanent power is a USB-C cable left plugged in. Check on the device before closing it up:

- whether it runs with the battery unplugged from the board (preferred: a lithium cell held at full charge for years in a closed case is not ideal);
- if the battery has to stay connected, that the charger stops charging at full and the case does not get warm.

Then tick **Mains power** on the display's page. Gen 2 boards report USB themselves; show that, but the tick box is what the server acts on, because gen 1 cannot tell.

### Firmware fork

Fork `usetrmnl/firmware` to `marctew/trmnl-firmware`. Keep the change small and behind the server's say-so, so the fork behaves exactly like stock against any other server.

- New optional field in the `/api/display` reply: `"poll_interval": 5` (seconds).
- When it is present and above 0, after drawing the firmware does **not** deep-sleep. It keeps Wi-Fi connected in modem sleep and every `poll_interval` seconds calls `GET /api/display` with `Update-Source: poll`.
  - Same `filename` as on the panel: do nothing.
  - New `filename`: download, draw, carry on polling.
  - Field missing or 0: go back to normal deep sleep with `refresh_rate`.
- Fall back to deep sleep after 10 failed polls in a row, and (gen 2) when USB power goes away.
- The existing 20-minute awake limit (`AWAKE_TIME_LIMIT_MS`) must not apply in this mode.
- Builds: `trmnl_4clr` and `trmnl_gen2_4clr`. Flash over USB as the firmware README describes.
- Stock firmware ignores the unknown field, so the server may send it to any device.

Server side: send `poll_interval` when `mains` is ticked. A `poll` call skips the wake-interval maths and telemetry history; it only updates `last_seen` and returns the current image. Online becomes "polled in the last 60 seconds". The footer drops "Next ~HH:MM".

Result: an override reaches the panel in one poll (5 s) plus the panel refresh (about 20 s), or a few seconds more if the item was not kept ready.

If verify-on-device item 3 comes out well, phase 1 on mains already gives about a minute, and this fork is a nicety, not a need. Decide after phase 1 is on the wall.

## Phase 3: more pages

- **Bins.** HomeForm's bin data is behind its login (`/api/dashboard/:id/bins`). Add `GET /api/public/bins` to HomeForm beside the meals one, returning the next collections for every configured bin widget, then a `pages/bins/` module here. A HomeForm change, so its own small spec.
- **Shopping list.** HomeForm already has `GET /api/public/shopping-list`; a module is cheap.
- **Remaining modules** checked for e-ink: Countdown, Sun and moon, Air quality, Service status, QR code, Photo frame.
- **Split screens on e-ink.** They should work through the renderer as they are; check and fix.

## Out of scope

- Serving firmware updates from Vital Signs.
- TRMNL X and other larger models (the model table makes room for them; nothing is built or tested).
- The firmware's "special functions" beyond telling it to sleep.
- Touch, buttons as input to Vital Signs (a button press already just wakes it early, which is useful: press to fetch a pending override).

## Done means

- A factory-reset BWRY pointed at the server shows up on the dashboard, is adopted with one click and draws its playlist.
- The panel's footer says when its data was checked, and turns red with the last good time when a source fails.
- Home Assistant shows battery, signal, last and next check-in, and what is on the panel; pulling the power produces an Online = off within one missed wake.
- `POST …/override {"content": "Meal plan"}` puts the meal plan on the panel at the next wake, and the playlist comes back when it expires.
- The Pi displays behave exactly as before, and their tests pass unchanged.
