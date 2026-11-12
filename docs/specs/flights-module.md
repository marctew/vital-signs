# Spec: Flights overhead module

A module that shows a live map of the aircraft around a place, with an overlay card describing one of them: photo, airline, type, registration, route, height and speed. It uses free, open ADS-B data, so it needs no account or key.

Read `CLAUDE.md` and DESIGN.md first. This spec follows the existing module pattern (`pages/weather/`, providers in `server/modules.py`) and adds nothing new to the agent, the protocol or the schema.

## Decisions already made

- **Data source: adsb.lol**, with airplanes.live as a fallback. Both serve the same readsb JSON format, are free and need no key. FR24 is out: Gold has no API, scraping it breaks their terms, and their paid API charges per flight.
- **No scraping and no logins.** Everything comes from documented public endpoints.
- **The server fetches, the page renders.** This is the same as every other module. The Pi only fetches map tiles and aircraft photos directly, because both have to be shown from their own servers anyway (see Attribution).
- **Leaflet for the map**, saved into the repo (no CDN, no build step).
- **The source sits behind a small dispatch function**, so a local receiver (readsb / tar1090 `aircraft.json`) can be added later without touching the page. Marc may get an RTL-SDR. Don't build the local source yet, but don't block it.

## Files

| Path | What |
|---|---|
| `pages/flights/module.json` | Manifest (below). `"data": "flights"`, `"can_hide": true`. |
| `pages/flights/index.html` | The page: map, aircraft markers, featured card, attribution line. |
| `pages/_shared/leaflet/` | Leaflet 1.9.4 (`leaflet.js`, `leaflet.css`, `LICENSE`), saved into the repo as-is. No marker images needed (we use divIcons). |
| `server/modules.py` | `fetch_flights` provider plus helpers, registered in `PROVIDERS` as `"flights": (fetch_flights, 5)`. |
| `server/data/airlines.json` | ICAO airline designator → `{name, iata}`, built from OpenFlights `airlines.dat` (active airlines only). |
| `tools/make_airlines.py` | Rebuilds `airlines.json` from a downloaded `airlines.dat`. |
| `tests/test_flights_module.py` + `tests/fixtures/adsblol_point.json` | Tests (below). |
| `README.md`, `DESIGN.md` §9 | Add Flights to the module lists. |

## Manifest options

```json
{
  "name": "Flights overhead",
  "description": "A live map of aircraft near a place, with details and a photo of one of them (from adsb.lol, no account needed).",
  "can_hide": true,
  "data": "flights",
  "options": [
    {"key": "title", "label": "Title (optional)", "type": "text", "default": ""},
    {"key": "location", "label": "Centre", "type": "text", "default": "", "required": true,
     "help": "A town (Towcester, UK) or a latitude and longitude (52.134, -0.990)"},
    {"key": "range_km", "label": "Range (km)", "type": "number", "default": 40, "min": 5, "max": 400},
    {"key": "min_alt", "label": "Lowest altitude to show (ft)", "type": "number", "default": 0, "min": 0, "max": 60000},
    {"key": "max_alt", "label": "Highest altitude to show (ft, 0 for no limit)", "type": "number", "default": 0, "min": 0, "max": 60000},
    {"key": "hide_ground", "label": "Hide aircraft on the ground", "type": "checkbox", "default": true},
    {"key": "featured", "label": "Details card", "type": "select", "default": "cycle", "choices": [
      {"value": "cycle", "label": "Each aircraft in turn, nearest first"},
      {"value": "nearest", "label": "Always the nearest"},
      {"value": "lowest", "label": "Always the lowest"},
      {"value": "off", "label": "Off, map only"}]},
    {"key": "cycle_seconds", "label": "Seconds per aircraft when taking turns", "type": "number", "default": 12, "min": 4, "max": 120},
    {"key": "photo", "label": "Show a photo of the aircraft", "type": "checkbox", "default": true},
    {"key": "route", "label": "Show where it is flying from and to", "type": "checkbox", "default": true},
    {"key": "labels", "label": "Callsign labels on the map", "type": "checkbox", "default": true},
    {"key": "trails", "label": "Show recent tracks", "type": "checkbox", "default": true},
    {"key": "map_style", "label": "Map", "type": "select", "default": "dark", "choices": [
      {"value": "dark", "label": "Dark (CARTO)"}, {"value": "light", "label": "Light (CARTO)"},
      {"value": "osm", "label": "OpenStreetMap"}]},
    {"key": "units", "label": "Units", "type": "select", "default": "aviation", "choices": [
      {"value": "aviation", "label": "Feet, knots, nautical miles"},
      {"value": "uk", "label": "Feet, mph, miles"},
      {"value": "metric", "label": "Metres, km/h, km"}]}
  ]
}
```

`background` and `accent` are added automatically (COMMON_OPTIONS). The accent colours the featured aircraft, the range ring and the card highlights.

## Server: `fetch_flights(cfg, content_id)`

### Centre

- `_centre(cfg)`: if `location` matches `^\s*-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?\s*$` and is a valid latitude/longitude, use it directly, with the place name `"52.134, -0.990"`. Otherwise call the existing `_geocode` (which caches).
- If the value is out of range or the geocode finds nothing, raise `ServiceError`. That gives a 400 response with a message the page can show.

### Source dispatch

```python
FLIGHT_SOURCES = {
    "adsblol": "https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{nm}",
    "airplaneslive": "https://api.airplanes.live/v2/point/{lat:.4f}/{lon:.4f}/{nm}",
}
```

- `nm = min(250, ceil(range_km / 1.852))`. Both APIs cap the radius at 250 nm, so for ranges above that, filter by distance afterwards. In practice nobody will go that wide.
- Try adsb.lol first. On `URLError`/`OSError`/`ValueError`, try airplanes.live. If both fail, let the exception reach `data()`, which already falls back to the last good result in memory, then on disk.
- `_flight_aircraft(cfg, lat, lon)` returns a list of **normalised** aircraft (below). That is where a `readsb` source with a local URL slots in later. Don't add a `source` option now.
- Verify on build: confirm the field names below against a live response from both APIs. Save one trimmed response as the test fixture.

### Normalising an aircraft

From the readsb `ac` array, keep only aircraft with `lat`/`lon` and `seen_pos < 60`:

| Out | From | Notes |
|---|---|---|
| `hex` | `hex` | Lower case. Strip a leading `~` (TIS-B/non-ICAO). Skip those for photos. |
| `callsign` | `flight` | Stripped; may be empty |
| `reg` | `r` | |
| `type` | `t` | ICAO type code, e.g. `A20N` |
| `type_name` | `desc` | e.g. "AIRBUS A-320neo". Title-case it on the page. |
| `operator` | `ownOp` | Fallback name when there is no airline match |
| `lat`, `lon` | | 4 dp |
| `alt` | `alt_baro`, else `alt_geom` | Feet, int. `alt_baro == "ground"` → `alt = 0`, `ground = true` |
| `gs` | `gs` | Knots |
| `track` | `track`, else `true_heading` | Degrees |
| `vrate` | `baro_rate`, else `geom_rate` | ft/min |
| `squawk` | `squawk` | |
| `category` | `category` | `A1`–`A7`, `B*`, `C*`: picks the icon |
| `military` | `dbFlags & 1` | |
| `emergency` | `emergency` not in (`none`, missing) **or** squawk in 7500/7600/7700 | |
| `dist_km`, `bearing` | Computed (haversine) from the centre | 1 dp, int |

Then:

- Filter by `dist_km <= range_km`, `hide_ground`, `min_alt`, and `max_alt` (when non-zero).
- Sort by distance and cap at **150** aircraft so a Pi isn't asked to draw a busy day at Heathrow with a 400 km range.

### Airline

- Take the callsign's first three letters, if they match `^[A-Z]{3}\d` and appear in `airlines.json`. Set `airline = {code, name, iata}`.
- Otherwise use `airline = None` and the page falls back to `operator`.
- Load the JSON once at import (lazily).

### Routes (adsb.lol routeset)

- `POST https://api.adsb.lol/api/0/routeset` with `{"planes": [{"callsign": "EZY12AB", "lat": 52.1, "lng": -0.9}, ...]}`. Send only callsigns not already in the route cache, at most 30 per call, and at most one call per fetch.
- Keep a module-level `_routes` dict: callsign → `(timestamp, route | None)`. Keep results for **6 h** and misses for **30 min**.
- `route = {"from": {"iata", "icao", "name", "city"}, "to": {...}}` from the response's `_airport_codes_iata` / `_airports`. When the response says `"plausible": false`, set `route = None`. Wrong routes on a wall screen look worse than none.
- Verify the response shape on build and put a fixture in the tests.
- Only when `cfg["route"]` is set. A failure here never fails the fetch: log it at debug level and leave the route as `None`.

### Photos (planespotters.net)

- `GET https://api.planespotters.net/pub/photos/hex/{hex}`.
- Keep the first photo's `thumbnail_large.src`, `link` and `photographer`. Cache them in `_photos`: **24 h** for hits, **6 h** for misses.
- To be polite, fetch at most **3 uncached hexes per fetch**, nearest first, from the first 10 aircraft. The cycle reaches the others on later fetches.
- Only when `cfg["photo"]` is set. Failures leave `photo = None`.
- The page hotlinks `src`. Planespotters' terms require the image to be shown from their server with the photographer credit and a link, so don't proxy it.

### Trails

- Keep a module-level `_trails` dict: `content_id → {hex: deque(maxlen=40)}` of `(t, lat, lon)`. Append on every successful fetch, skipping points that haven't moved.
- Prune hexes not seen for 5 min.
- Return `trail: [[lat, lon], ...]` per aircraft when `cfg["trails"]` is set, rounded to 4 dp.
- A trail is lost on a restart. That's fine.

### Response

```json
{
  "centre": {"lat": 52.134, "lon": -0.99, "place": "Towcester"},
  "range_km": 40,
  "source": "adsb.lol",
  "fetched": 1762950000,
  "aircraft": [
    {"hex": "4ca7b5", "callsign": "RYR4AB", "reg": "EI-DCL", "type": "B738", "type_name": "BOEING 737-800",
     "operator": "RYANAIR", "airline": {"code": "RYR", "name": "Ryanair", "iata": "FR"},
     "lat": 52.2, "lon": -0.91, "alt": 34000, "ground": false, "gs": 452, "track": 312, "vrate": 0,
     "squawk": "2341", "category": "A3", "military": false, "emergency": false,
     "dist_km": 8.4, "bearing": 38,
     "route": {"from": {"iata": "STN", "name": "London Stansted", "city": "London"}, "to": {"iata": "DUB", "name": "Dublin", "city": "Dublin"}},
     "photo": {"src": "https://t.plnspttrs.net/...", "link": "https://www.planespotters.net/photo/...", "photographer": "J. Smith"},
     "trail": [[52.18, -0.88], [52.19, -0.9]]}
  ]
}
```

`source` names whichever API answered, for the attribution line.

### Load

- `data()` caches each item for 5 s, so one upstream call is made per item every 5 s, however many displays show it.
- `data()` also writes the disk cache on every fresh fetch. At this rate that's a small JSON write every 5 s. Acceptable, but if it shows up in I/O, change `data()` to skip the disk write for providers with a TTL under 30 s (and add a test).
- The request timeout stays at `TIMEOUT` (10 s).

## Page: `pages/flights/index.html`

### Layout

- The map fills the module box: a Leaflet map in `.m-body`, plus the `m-title` when a title is set.
- `dragging`, `zoomControl`, scroll and touch zoom are all off. It's a display, not a map app.
- Fit the view to the range circle's bounds with a little padding, and refit on `VS.onResize`.
- **Featured card:**
  - `data-shape="tall"`: across the bottom, about 30% of the height.
  - `wide`: a panel on the right, about 32% of the width.
  - `square`: bottom, compact.
  - Use `calc(var(--s) * N)` throughout. Nothing assumes portrait or a resolution.
- **Card content**, top to bottom:
  - Photo, 16:9 `object-fit: cover`, hidden when there is none. Credit in small text under it: `© photographer · planespotters.net`.
  - Airline name (or operator, or "Private") in large text, with the callsign in the accent colour.
  - Type name, then `reg · ICAO type`.
  - Route, when present: `STN London Stansted → DUB Dublin`. Big IATA codes, smaller names, wrapping cleanly.
  - A row of figures: altitude, a climb/descent arrow from `vrate` (±300 ft/min dead band), speed, distance and compass direction from the centre ("8 km NE").
  - Military and emergency badges: an emergency badge uses the existing bad/red colour.
- **Attribution**, a single small muted line along the bottom of the map: `Aircraft: adsb.lol · Map © OpenStreetMap contributors © CARTO`. Swap the source name when airplanes.live answered, and drop CARTO for the OSM style.

### Map layers

- Tiles:
  - `dark`: `https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png`
  - `light`: `.../light_all/...`
  - `osm`: `https://tile.openstreetmap.org/{z}/{x}/{y}.png`
- A centre marker (small accent dot), the range ring (accent, thin, dashed) and a half-range ring (fainter).
- **Aircraft** are `L.marker` with an `L.divIcon` holding an inline SVG silhouette rotated by `track`:
  - Icons by category: light (A1/A2), airliner (A3/A4), heavy (A5), rotorcraft (A7), and a generic shape for anything else. Ground vehicles never appear, since ground traffic is filtered or shown as a dot.
  - Size scales with `--s`.
  - Colour by altitude band (ground / <5k / <15k / <30k / above), using a fixed, colour-blind-safe ramp declared as CSS variables.
  - The featured aircraft is drawn in the accent colour, larger, with a soft halo.
  - Military is outlined; emergency is red and pulses.
- **Labels** (option): callsign or reg, plus flight level (`FL340`, or `3,200 ft` below 6,000 ft) next to each icon. Hide labels when there are more than 40 aircraft, so the map doesn't turn to mush.
- **Trails** (option): a polyline per aircraft, faint, at about 40% opacity. The featured aircraft's trail is drawn in the accent colour.
- **Smooth movement:** between polls, dead-reckon each aircraft from `gs` and `track` for at most 6 s with `requestAnimationFrame`, throttled to about 4 fps. On the next poll, snap to the real position if it is more than 300 m off, otherwise ease towards it. Keep it cheap: move markers with `setLatLng` and don't rebuild them.
- Diff the markers by `hex` on each poll: add new ones, update existing ones, remove those that have gone.

### Featured selection

- `cycle`: work through the aircraft list (already nearest first) for `cycle_seconds` each. Skip aircraft that have left; if the current one leaves, move on early. Start again from the nearest after the last.
- `nearest`, `lowest`: re-pick on every poll, keeping the current choice unless another is closer or lower by a margin (1 km or 500 ft), so the card doesn't flicker between two aircraft.
- **An emergency always wins** the card whatever the mode, and stays until its emergency ends.
- `off`: no card and no highlight.
- Preload the next photo (`new Image()`) a couple of seconds before the cycle advances, so the card changes in one go.

### Polling and empty state

- `VS.module()` → `VS.every(5, load)`. Use `VS.moduleData()`; on error, keep the last data and use `VS.retrySoon`, as the weather page does.
- When there are no aircraft: show the map still, with the card replaced by a quiet "No aircraft within 40 km" (in the configured units), and call `VS.setActive(false)` so a split screen can give the space away. Call `VS.setActive(true)` when aircraft return.
- Number formatting: `toLocaleString('en-GB')`. Distances, speeds and heights follow `units`.

## Attribution and terms (must ship with the module)

- adsb.lol data is ODbL: name it on screen. airplanes.live: name it when it is the source.
- CARTO basemaps are free for non-commercial use, with "© OpenStreetMap contributors © CARTO" shown. OSM's own tiles fall under the OSM tile usage policy (fine for one or two screens, and browsers cache tiles).
- Planespotters photos: hotlinked, with the photographer's name and planespotters.net shown.
- OpenFlights airline data is ODbL: credit it in `server/data/README.md` alongside the JSON.
- adsb.lol has said it may introduce API keys. If it does, add an optional `secret` option (`adsblol_key`) and send it as their docs say. Not needed now.

## Tests: `tests/test_flights_module.py`

Monkeypatch `modules._get` (and the POST helper used for routeset) to return fixtures. Never hit the network.

1. **Normalising:** the fixture gives the expected fields. `alt_baro: "ground"` → `ground=True, alt=0`. `~` hexes are kept but get no photo lookup. Positions older than 60 s are dropped.
2. **Filters:** range, min/max altitude, hide ground, the 150 cap, and nearest-first order.
3. **Centre parsing:** `"52.134, -0.990"` is used directly with no geocode call. `"95, 10"` raises `ServiceError`. A place name goes through `_geocode`.
4. **Fallback:** when adsb.lol raises `URLError`, airplanes.live is used and `source` says so. When both fail, `/api/module/<id>/data` returns the previous result. Use the `server` fixture to create the item through `save_module` / the form.
5. **Routes:** cached callsigns aren't re-requested. Misses expire after 30 min (patch `time.time`). Implausible routes come back as `None`. A routeset failure doesn't fail the fetch.
6. **Photos:** at most 3 lookups per fetch, misses are cached, and `photo` is off means no lookups at all.
7. **Trails:** points accumulate across fetches, unmoved points aren't duplicated, and stale hexes are pruned.
8. **Airline lookup:** `RYR4AB` → Ryanair, `GABCD` (a registration-style callsign) → `None`.
9. **Manifest:** `list_modules()` includes `flights` with every option type valid, and the page and config endpoints answer for a saved item.

## Verify on device

Write the results to `docs/on-device-verification.md`:

- CPU and frame pacing on the Pi with the module full screen on a portrait 1080 × 2560 output, about 60 aircraft, labels and trails on. If it's heavy, reduce the animation frame rate before cutting features.
- The same in a split-screen pane, with another module animating alongside.
- That tiles and Planespotters images load from the Pi's network, with no blocked hosts.

## Later (do not build, do not block)

- **Local receiver source:** `source` select plus a `receiver` URL (option `setting: "adsb_receiver_url"` like Frigate's). Read `{url}/data/aircraft.json` from readsb/tar1090: the same format as the APIs above, with no distance filter upstream, so filter here. Keep adsb.lol for routes. Once feeding, FR24 gives a free Business subscription.
- **Home Assistant:** an MQTT sensor for "aircraft overhead" / nearest aircraft via `server/ha_mqtt.py`.
- Airport markers and runway outlines for local airfields (Sywell, Silverstone heliport, Luton).
