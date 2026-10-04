"""Modules: configurable local pages (clock, calendar, RSS, Plex, weather).

A module is a folder in pages/ with a module.json manifest that lists its
options. A content item of kind "module" stores one configuration of it, and
its page loads at /pages/<module>/?m=<content id>. The page asks this server
for its options and, for modules that need outside data, for the data itself:
the server fetches it with the stored settings, so feed URLs and tokens never
reach page source and nothing here fetches a URL an admin did not configure.
"""
import email.utils
import hashlib
import html
import json
import logging
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlencode, urlsplit

from flask import Blueprint, Response, current_app, jsonify, request

from . import localpages
from .db import get_db
from .services import ServiceError

bp = Blueprint("modules", __name__)
log = logging.getLogger("server.modules")

TIMEOUT = 10
MAX_BYTES = 5 * 1024 * 1024
OPTION_TYPES = ("text", "number", "select", "checkbox", "list", "secret", "color")

# Offered by every module.
COMMON_OPTIONS = [
    {"key": "background", "label": "Background colour", "type": "color", "default": "#0b0f14"},
    {"key": "accent", "label": "Accent colour", "type": "color", "default": "#4cc2ff"},
]

_cache = {}
_cache_lock = threading.Lock()


# --- manifests and configuration ---------------------------------------------

def list_modules():
    """{folder name: manifest} for every page folder in the repo that has a module.json."""
    modules = {}
    if not localpages.REPO_PAGES.is_dir():
        return modules
    for folder in sorted(localpages.REPO_PAGES.iterdir()):
        manifest = folder / "module.json"
        if not manifest.is_file() or not localpages.NAME_RE.match(folder.name):
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError:
            continue
        data["id"] = folder.name
        data["options"] = [o for o in data.get("options", []) if o.get("type") in OPTION_TYPES] + COMMON_OPTIONS
        modules[folder.name] = data
    return modules


def is_module(name):
    return (localpages.REPO_PAGES / name / "module.json").is_file()


def read_form(manifest, form, previous=None):
    """Turn a submitted options form into a config dict, validated against the manifest."""
    previous = previous or {}
    config = {}
    for option in manifest["options"]:
        key, kind = option["key"], option["type"]
        raw = form.get(f"opt_{key}", "")
        if kind == "checkbox":
            value = bool(form.get(f"opt_{key}"))
        elif kind == "number":
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = option.get("default", 0)
            value = min(option.get("max", value), max(option.get("min", value), value))
            value = int(value) if float(value).is_integer() else value
        elif kind == "select":
            choices = [c["value"] for c in option.get("choices", [])]
            value = raw if raw in choices else option.get("default")
        elif kind == "list":
            value = [line.strip() for line in raw.splitlines() if line.strip()]
        elif kind == "secret":
            value = raw.strip() or previous.get(key, "")     # blank keeps the stored secret
        elif kind == "color":
            value = raw if re.fullmatch(r"#[0-9a-fA-F]{6}", raw or "") else option.get("default")
        else:
            value = raw.strip()
        if option.get("required") and not value:
            raise ServiceError(f"{option['label']} is required")
        config[key] = value
    return config


def save_module(db, content_id, module_name, name, form):
    """Create a module content item (content_id None) or update one. Returns its id."""
    manifest = list_modules().get(module_name)
    if manifest is None:
        raise ServiceError("No such module", 404)
    name = (name or "").strip()
    if not name:
        raise ServiceError("Name is required")
    previous = {}
    if content_id is not None:
        row = db.execute("SELECT * FROM content_items WHERE id = ? AND kind = 'module' AND module = ?",
                         (content_id, module_name)).fetchone()
        if row is None:
            raise ServiceError("No such module item", 404)
        previous = json.loads(row["config"] or "{}")
    config = json.dumps(read_form(manifest, form, previous))
    if content_id is None:
        content_id = db.execute("INSERT INTO content_items (name, url, kind, module, config)"
                                " VALUES (?, '', 'module', ?, ?)", (name, module_name, config)).lastrowid
        db.execute("UPDATE content_items SET url = ? WHERE id = ?",
                   (f"/pages/{module_name}/?m={content_id}", content_id))
    else:
        db.execute("UPDATE content_items SET name = ?, config = ? WHERE id = ?", (name, config, content_id))
    db.commit()
    return content_id


def _instance(content_id):
    """(manifest, config with defaults filled in) for a module content item, or (None, None)."""
    row = get_db().execute("SELECT * FROM content_items WHERE id = ? AND kind = 'module'",
                           (content_id,)).fetchone()
    manifest = list_modules().get(row["module"]) if row else None
    if manifest is None:
        return None, None
    stored = json.loads(row["config"] or "{}")
    config = {o["key"]: stored.get(o["key"], o.get("default", "")) for o in manifest["options"]}
    return manifest, config


# --- endpoints the module pages call ------------------------------------------

@bp.get("/api/module/<int:content_id>/config")
def config(content_id):
    manifest, cfg = _instance(content_id)
    if manifest is None:
        return jsonify(error="unknown module item"), 404
    hidden = {o["key"] for o in manifest["options"] if o["type"] == "secret" or o.get("private")}
    return jsonify(module=manifest["id"], options={k: v for k, v in cfg.items() if k not in hidden})


def _failure(error):
    """A short reason a fetch failed, fit to show on a screen."""
    if isinstance(error, urllib.error.HTTPError):
        return f"it answered with error {error.code}"
    reason = getattr(error, "reason", error)
    if isinstance(reason, TimeoutError) or "timed out" in str(reason):
        return "it did not answer in time"
    if isinstance(reason, OSError) and getattr(reason, "errno", None) in (-2, -3, 11001, 11002):
        return "its address could not be looked up (DNS)"
    if isinstance(error, (ValueError, KeyError, ET.ParseError)):
        return "its reply was not understood"
    return "the connection failed"


def _disk_cache(content_id, key):
    folder = current_app.config["VS"]["data_dir"] / "module-cache"
    folder.mkdir(exist_ok=True)
    return folder / f"{content_id}-{key}.json"


@bp.get("/api/module/<int:content_id>/data")
def data(content_id):
    manifest, cfg = _instance(content_id)
    provider = PROVIDERS.get(manifest.get("data")) if manifest else None
    if provider is None:
        return jsonify(error="this module has no data"), 404
    fetch, ttl = provider
    digest = hashlib.sha1(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]
    key = (content_id, digest)
    with _cache_lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return jsonify(hit[1])
    saved = _disk_cache(content_id, digest)
    try:
        result = fetch(cfg, content_id)
    except ServiceError as e:
        return jsonify(error=str(e)), 400
    except (urllib.error.URLError, OSError, ValueError, KeyError, ET.ParseError) as e:
        log.warning("%s item %s: fetch failed: %r", manifest["id"], content_id, e)
        # Old data beats an error on a wall display: first what is in memory, then what
        # was saved before the server last restarted.
        if hit:
            return jsonify(hit[1])
        try:
            return jsonify(json.loads(saved.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return jsonify(error=f"Could not load {manifest['name'].lower()} data: {_failure(e)}."), 502
    with _cache_lock:
        _cache[key] = (time.time(), result)
    try:
        for old in saved.parent.glob(f"{content_id}-*.json"):     # settings changed: drop the old copy
            if old != saved:
                old.unlink()
        saved.write_text(json.dumps(result), encoding="utf-8")
    except OSError:
        pass
    return jsonify(result)


@bp.get("/api/module/<int:content_id>/image")
def image(content_id):
    """Plex artwork, fetched with the stored token. Only library paths on the configured server."""
    manifest, cfg = _instance(content_id)
    path = request.args.get("path", "")
    if (manifest is None or manifest.get("data") != "plex" or ".." in path
            or not re.fullmatch(r"/library/[\w/.-]+", path)):
        return jsonify(error="not found"), 404
    headers = {"X-Plex-Token": cfg.get("token", "")}
    try:
        base = _plex_base(cfg)
        body = None
        width = request.args.get("w", type=int)
        if width and 50 <= width <= 1600:
            # Let Plex scale the picture down: a wall of full-size posters is heavy for a Pi.
            try:
                body, mimetype = _get(f"{base}/photo/:/transcode?" + urlencode({
                    "width": width, "height": width * 3 // 2, "minSize": 1, "url": path}), headers)
            except (urllib.error.URLError, OSError, ValueError):
                body = None
        if body is None:
            body, mimetype = _get(f"{base}{path}", headers)
    except (urllib.error.URLError, OSError, ValueError, ServiceError):
        return jsonify(error="not available"), 502
    if not mimetype.startswith("image/"):
        return jsonify(error="not an image"), 502
    resp = Response(body, mimetype=mimetype)
    resp.headers["Cache-Control"] = "max-age=86400"
    return resp


def _get(url, headers=None):
    if not url.startswith(("http://", "https://")):
        raise ServiceError("Addresses must start with http:// or https://")
    req = urllib.request.Request(url, headers={"User-Agent": "vitalsigns", **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = r.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("response too large")
        return body, r.headers.get_content_type()


# --- RSS ------------------------------------------------------------------------

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"


def _plain(text, limit=400):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _feed_date(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def parse_feed(body):
    """Items of an RSS or Atom document as dicts."""
    root = ET.fromstring(body)
    items = []
    if root.tag == f"{ATOM}feed":
        source = _plain(root.findtext(f"{ATOM}title"))
        for entry in root.iter(f"{ATOM}entry"):
            link = next((l.get("href") for l in entry.findall(f"{ATOM}link")
                         if l.get("rel", "alternate") == "alternate"), "")
            thumb = entry.find(f"{MEDIA}thumbnail")
            items.append({
                "source": source, "title": _plain(entry.findtext(f"{ATOM}title")), "link": link,
                "summary": _plain(entry.findtext(f"{ATOM}summary") or entry.findtext(f"{ATOM}content")),
                "published": _feed_date(entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated")),
                "image": thumb.get("url") if thumb is not None else None})
        return items
    source = _plain(root.findtext("channel/title"))
    for item in root.iter("item"):
        image = None
        for tag in (f"{MEDIA}thumbnail", f"{MEDIA}content", "enclosure"):
            el = item.find(tag)
            if el is not None and el.get("url") and (tag != "enclosure" or el.get("type", "").startswith("image/")):
                image = el.get("url")
                break
        items.append({
            "source": source, "title": _plain(item.findtext("title")), "link": item.findtext("link") or "",
            "summary": _plain(item.findtext("description")),
            "published": _feed_date(item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date")),
            "image": image})
    return items


def fetch_rss(cfg, content_id):
    feeds = cfg.get("feeds") or []
    if not feeds:
        raise ServiceError("No feeds configured")
    items, failed = [], 0
    for url in feeds[:10]:
        try:
            items += parse_feed(_get(url)[0])
        except (urllib.error.URLError, OSError, ValueError, ET.ParseError):
            failed += 1
    if failed == len(feeds[:10]):
        raise ValueError("no feed could be loaded")
    items = [i for i in items if i["title"]]
    items.sort(key=lambda i: i["published"] or "", reverse=True)
    return {"items": items[:40]}


# --- calendar -------------------------------------------------------------------

def _when(value):
    """(ISO string, all_day, sort key) for an iCalendar date or datetime."""
    if isinstance(value, datetime):
        key = value.astimezone().replace(tzinfo=None) if value.tzinfo else value
        return value.isoformat(), False, key
    return value.isoformat(), True, datetime(value.year, value.month, value.day)


def parse_calendar(body, start, end):
    """Events between two datetimes, with recurring events expanded."""
    import icalendar
    import recurring_ical_events
    calendar = icalendar.Calendar.from_ical(body)
    name = str(calendar.get("X-WR-CALNAME", ""))
    events = []
    for event in recurring_ical_events.of(calendar).between(start, end):
        if "DTSTART" not in event:
            continue
        starts = event["DTSTART"].dt
        ends = event["DTEND"].dt if "DTEND" in event else starts
        start_iso, all_day, key = _when(starts)
        events.append({"title": str(event.get("SUMMARY", "")) or "Busy", "start": start_iso,
                       "end": _when(ends)[0], "all_day": all_day,
                       "location": str(event.get("LOCATION", "")), "calendar": name, "_key": key})
    return events


def fetch_calendar(cfg, content_id):
    urls = cfg.get("calendars") or []
    if not urls:
        raise ServiceError("No calendars configured")
    start = datetime.now() - timedelta(days=1)
    end = datetime.now() + timedelta(days=int(cfg.get("days") or 7) + 1)
    events, failed = [], 0
    for url in urls[:10]:
        try:
            events += parse_calendar(_get(re.sub(r"^webcal://", "https://", url))[0], start, end)
        except (urllib.error.URLError, OSError, ValueError):
            failed += 1
    if failed == len(urls[:10]):
        raise ValueError("no calendar could be loaded")
    events.sort(key=lambda e: (e["_key"], not e["all_day"], e["title"]))
    for event in events:
        del event["_key"]
    return {"events": events[:200]}


# --- Plex -----------------------------------------------------------------------

def _plex_base(cfg):
    base = (cfg.get("server") or "").rstrip("/")
    if not base:
        raise ServiceError("No Plex server configured")
    return base


def parse_plex_sessions(payload, users, content_id):
    """Now-playing entries from Plex's /status/sessions JSON, limited to the given user names."""
    wanted = {u.lower() for u in users or []}
    sessions = []
    for m in payload.get("MediaContainer", {}).get("Metadata") or []:
        user = (m.get("User") or {}).get("title", "")
        if wanted and user.lower() not in wanted:
            continue
        kind = m.get("type", "")
        if kind == "episode":
            title = m.get("grandparentTitle", "")
            subtitle = f"S{m.get('parentIndex', '?')} E{m.get('index', '?')} · {m.get('title', '')}"
        elif kind == "track":
            title, subtitle = m.get("title", ""), m.get("grandparentTitle", "")
        else:
            title, subtitle = m.get("title", ""), str(m.get("year", "") or "")
        art = m.get("grandparentThumb") or m.get("parentThumb") or m.get("thumb")
        duration = float(m.get("duration") or 0)
        sessions.append({
            "user": user, "type": kind, "title": title, "subtitle": subtitle,
            "summary": _plain(m.get("summary"), 700),
            "state": (m.get("Player") or {}).get("state", ""),
            "player": (m.get("Player") or {}).get("title", ""),
            "progress": round(float(m.get("viewOffset") or 0) / duration, 4) if duration else 0,
            "image": f"/api/module/{content_id}/image?path={quote(art)}" if art else None})
    return sessions


def parse_plex_recent(payload, content_id, library=""):
    """Recently added entries from a library's /recentlyAdded JSON, one per film, show or album."""
    items = []
    for m in payload.get("MediaContainer", {}).get("Metadata") or []:
        kind = m.get("type", "")
        if kind == "episode":
            title, art = m.get("grandparentTitle", ""), m.get("grandparentThumb") or m.get("parentThumb") or m.get("thumb")
        elif kind == "season":
            title, art = m.get("parentTitle", ""), m.get("parentThumb") or m.get("thumb")
        elif kind in ("album", "track"):
            title, art = m.get("parentTitle") or m.get("title", ""), m.get("thumb") or m.get("parentThumb")
        else:
            title, art = m.get("title", ""), m.get("thumb")
        if not title or not art:
            continue
        items.append({"title": title, "library": library, "added": int(m.get("addedAt") or 0),
                      "image": f"/api/module/{content_id}/image?path={quote(art)}"})
    return items


_recent = {}
RECENT_SECONDS = 600
RECENT_LIMIT = 24


def _plex_recent(cfg, content_id, base, headers):
    """The newest items across the chosen libraries. Cached: libraries change slowly."""
    wanted = {name.lower() for name in cfg.get("libraries") or []}
    key = (content_id, base, tuple(sorted(wanted)))
    hit = _recent.get(key)
    if hit and time.time() - hit[0] < RECENT_SECONDS:
        return hit[1]
    sections = json.loads(_get(f"{base}/library/sections", headers)[0])["MediaContainer"].get("Directory") or []
    items = []
    for section in sections:
        name = section.get("title", "")
        if section.get("type") not in ("movie", "show", "artist") or (wanted and name.lower() not in wanted):
            continue
        body, _ = _get(f"{base}/library/sections/{section['key']}/recentlyAdded"
                       "?X-Plex-Container-Start=0&X-Plex-Container-Size=40", headers)
        items += parse_plex_recent(json.loads(body), content_id, name)
    items.sort(key=lambda i: i["added"], reverse=True)
    seen, newest = set(), []
    for item in items:      # several new episodes of one show are one entry
        if item["title"] not in seen:
            seen.add(item["title"])
            newest.append(item)
    newest = newest[:RECENT_LIMIT]
    _recent[key] = (time.time(), newest)
    return newest


def fetch_plex(cfg, content_id):
    base = _plex_base(cfg)
    headers = {"X-Plex-Token": cfg.get("token", ""), "Accept": "application/json"}
    body, _ = _get(f"{base}/status/sessions", headers)
    result = {"sessions": parse_plex_sessions(json.loads(body), cfg.get("users"), content_id)}
    if cfg.get("idle_mode") == "recent":
        try:
            result["recent"] = _plex_recent(cfg, content_id, base, headers)
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            result["recent"] = []
    return result


# --- weather (Open-Meteo, no key needed) ------------------------------------------

_places = {}
COUNTRY_ALIASES = {"uk": "gb", "usa": "us", "england": "gb", "scotland": "gb", "wales": "gb"}


def _geocode(location):
    key = location.strip().lower()
    if key in _places:
        return _places[key]
    name, _, region = (part.strip() for part in location.partition(","))
    body, _ = _get("https://geocoding-api.open-meteo.com/v1/search?" + urlencode({"name": name, "count": 10}))
    results = json.loads(body).get("results") or []
    if not results:
        raise ServiceError(f"Could not find a place called {location}")
    place = results[0]
    if region:
        want = COUNTRY_ALIASES.get(region.lower(), region.lower())
        place = next((r for r in results if want in (
            str(r.get("country", "")).lower(), str(r.get("country_code", "")).lower(),
            str(r.get("admin1", "")).lower(), str(r.get("admin2", "")).lower())), place)
    _places[key] = (place["latitude"], place["longitude"], place["name"])
    return _places[key]


def fetch_weather(cfg, content_id):
    location = (cfg.get("location") or "").strip()
    if not location:
        raise ServiceError("No location configured")
    latitude, longitude, place = _geocode(location)
    days = int(cfg.get("days") or 5)
    body, _ = _get("https://api.open-meteo.com/v1/forecast?" + urlencode({
        "latitude": latitude, "longitude": longitude, "timezone": "auto", "forecast_days": days,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "temperature_unit": "fahrenheit" if cfg.get("units") == "fahrenheit" else "celsius",
        "wind_speed_unit": cfg.get("wind") if cfg.get("wind") in ("mph", "kmh", "ms") else "mph"}))
    raw = json.loads(body)
    current, daily = raw["current"], raw["daily"]
    return {
        "place": place,
        "current": {"temperature": current["temperature_2m"], "feels_like": current["apparent_temperature"],
                    "code": current["weather_code"], "wind": current["wind_speed_10m"],
                    "is_day": bool(current["is_day"])},
        "daily": [{"date": daily["time"][i], "code": daily["weather_code"][i],
                   "max": daily["temperature_2m_max"][i], "min": daily["temperature_2m_min"][i],
                   "rain": daily["precipitation_probability_max"][i]} for i in range(len(daily["time"]))],
    }


# --- Frigate ----------------------------------------------------------------------

def parse_frigate_config(config, wanted):
    """Cameras from Frigate's /api/config: name, picture size and the go2rtc stream for live video.

    `wanted` limits and orders the result; empty means every enabled camera.
    `stream` is None when Frigate has no restream for a camera, which then shows pictures.
    """
    streams = set((config.get("go2rtc") or {}).get("streams") or {})
    cameras = {}
    for name, camera in (config.get("cameras") or {}).items():
        if camera.get("enabled") is False:
            continue
        live = camera.get("live") or {}
        # Frigate 0.15+ has live.streams {label: stream}; earlier versions have live.stream_name.
        candidates = list((live.get("streams") or {}).values()) + [live.get("stream_name"), name]
        stream = next((s for s in candidates if s and s in streams), None)
        detect = camera.get("detect") or {}
        cameras[name] = {"name": name, "stream": stream,
                         "width": detect.get("width") or 0, "height": detect.get("height") or 0}
    if wanted:
        lookup = {name.lower(): cam for name, cam in cameras.items()}
        return [lookup[w.lower()] for w in wanted if w.lower() in lookup]
    return list(cameras.values())


def fetch_frigate(cfg, content_id):
    base = (cfg.get("frigate") or "").rstrip("/")
    if not base:
        raise ServiceError("No Frigate address configured")
    body, _ = _get(f"{base}/api/config")
    return {"cameras": parse_frigate_config(json.loads(body), cfg.get("cameras"))}


# --- service status -----------------------------------------------------------------

MAX_SERVICES = 40
_since = {}     # (content id, service name) -> (up, when it last changed)


def check_service(target, timeout=5, auth_ok=True, insecure=True):
    """Whether one service answers: (up, milliseconds or None, short reason when down).

    http(s):// addresses are fetched; tcp://host:port only has to accept a connection.
    """
    started = time.monotonic()
    try:
        parts = urlsplit(target)
        if parts.scheme == "tcp" and parts.hostname and parts.port:
            socket.create_connection((parts.hostname, parts.port), timeout=timeout).close()
            code = None
        elif parts.scheme in ("http", "https") and parts.hostname:
            context = ssl._create_unverified_context() if insecure and parts.scheme == "https" else None
            req = urllib.request.Request(target, headers={"User-Agent": "vitalsigns"})
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=context) as r:
                    code = r.status
            except urllib.error.HTTPError as e:
                code = e.code
        else:
            return False, None, "bad address"
    except (OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            return False, None, "no answer"
        if isinstance(reason, ConnectionRefusedError):
            return False, None, "refused"
        if isinstance(reason, socket.gaierror):
            return False, None, "name not found"
        if isinstance(reason, ssl.SSLError):
            return False, None, "certificate problem"
        return False, None, "unreachable"
    elapsed = round((time.monotonic() - started) * 1000)
    if code is not None and code >= 400 and not (auth_ok and code in (401, 403)):
        return False, elapsed, f"error {code}"
    return True, elapsed, ""


def parse_services(lines):
    """[(name, address)] from "Name | address" lines. A line with no name is named after its host."""
    services = []
    for line in lines or []:
        name, sep, target = (part.strip() for part in line.partition("|"))
        if not sep:
            name, target = "", name
        if not target:
            continue
        services.append((name or urlsplit(target).hostname or target, target))
    return services[:MAX_SERVICES]


def fetch_status(cfg, content_id):
    services = parse_services(cfg.get("services"))
    if not services:
        raise ServiceError("No services configured")
    timeout = float(cfg.get("timeout") or 5)
    with ThreadPoolExecutor(max_workers=min(12, len(services))) as pool:
        results = list(pool.map(
            lambda s: check_service(s[1], timeout, bool(cfg.get("auth_ok", True)), bool(cfg.get("insecure", True))),
            services))
    now = time.time()
    report = []
    for (name, _), (up, ms, detail) in zip(services, results):
        last = _since.get((content_id, name))
        if last is None or last[0] != up:
            last = _since[(content_id, name)] = (up, now)
        report.append({"name": name, "up": up, "ms": ms, "detail": detail, "since": round(last[1])})
    return {"services": report, "checked": round(now)}


# data type in module.json -> (fetch function, cache seconds)
PROVIDERS = {
    "rss": (fetch_rss, 300),
    "calendar": (fetch_calendar, 300),
    "plex": (fetch_plex, 8),
    "weather": (fetch_weather, 900),
    "frigate": (fetch_frigate, 300),
    "status": (fetch_status, 25),
}
