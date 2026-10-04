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
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from flask import Blueprint, Response, jsonify, request

from . import localpages
from .db import get_db
from .services import ServiceError

bp = Blueprint("modules", __name__)

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


@bp.get("/api/module/<int:content_id>/data")
def data(content_id):
    manifest, cfg = _instance(content_id)
    provider = PROVIDERS.get(manifest.get("data")) if manifest else None
    if provider is None:
        return jsonify(error="this module has no data"), 404
    fetch, ttl = provider
    key = (content_id, hashlib.sha1(json.dumps(cfg, sort_keys=True).encode()).hexdigest())
    with _cache_lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return jsonify(hit[1])
    try:
        result = fetch(cfg, content_id)
    except ServiceError as e:
        return jsonify(error=str(e)), 400
    except (urllib.error.URLError, OSError, ValueError, KeyError, ET.ParseError) as e:
        if hit:     # stale data beats an error on a wall display
            return jsonify(hit[1])
        return jsonify(error="Could not reach the data source. Check this module's settings."), 502
    with _cache_lock:
        _cache[key] = (time.time(), result)
    return jsonify(result)


@bp.get("/api/module/<int:content_id>/image")
def image(content_id):
    """Plex artwork, fetched with the stored token. Only library paths on the configured server."""
    manifest, cfg = _instance(content_id)
    path = request.args.get("path", "")
    if (manifest is None or manifest.get("data") != "plex" or ".." in path
            or not re.fullmatch(r"/library/[\w/.-]+", path)):
        return jsonify(error="not found"), 404
    try:
        body, mimetype = _get(f"{_plex_base(cfg)}{path}", {"X-Plex-Token": cfg.get("token", "")})
    except (urllib.error.URLError, OSError, ServiceError):
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
            "state": (m.get("Player") or {}).get("state", ""),
            "player": (m.get("Player") or {}).get("title", ""),
            "progress": round(float(m.get("viewOffset") or 0) / duration, 4) if duration else 0,
            "image": f"/api/module/{content_id}/image?path={quote(art)}" if art else None})
    return sessions


def fetch_plex(cfg, content_id):
    body, _ = _get(f"{_plex_base(cfg)}/status/sessions",
                   {"X-Plex-Token": cfg.get("token", ""), "Accept": "application/json"})
    return {"sessions": parse_plex_sessions(json.loads(body), cfg.get("users"), content_id)}


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


# data type in module.json -> (fetch function, cache seconds)
PROVIDERS = {
    "rss": (fetch_rss, 300),
    "calendar": (fetch_calendar, 300),
    "plex": (fetch_plex, 8),
    "weather": (fetch_weather, 900),
}
