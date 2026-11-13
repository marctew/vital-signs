"""Modules: configurable local pages (clock, calendar, RSS, Plex, weather, 3D printers).

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
import io
import json
import logging
import math
import os
import re
import shutil
import socket
import ssl
import subprocess
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
OPTION_TYPES = ("text", "number", "select", "checkbox", "list", "secret", "color", "cameras", "printers")

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
        elif kind == "cameras":
            # Ticked cameras (with their labels) win; the text box is for ordering or typing by hand.
            typed = [line.strip() for line in raw.splitlines() if line.strip()]
            picked = []
            for name in form.getlist(f"opt_{key}_pick"):
                label = (form.get(f"opt_{key}_label_{name}") or "").strip()
                picked.append(f"{name} | {label}" if label else name)
            value = typed or picked
        elif kind == "printers":
            # One printer from the dropdown (or typed when Home Assistant could not be asked); blank is all.
            value = [p for p, _ in parse_printer_lines([raw])]
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
    for option in manifest["options"]:
        if option.get("setting") and not config.get(option["key"]):
            config[option["key"]] = setting(option["setting"])
    return manifest, config


def setting(key, default=""):
    """A value from the Settings page (the settings table), or `default`."""
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


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
    """Plex artwork or a printer picture, fetched with the stored token. Only library paths on the configured server."""
    manifest, cfg = _instance(content_id)
    if manifest is not None and manifest.get("data") == "printer":
        return printer_image(cfg)
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

def parse_camera_lines(lines):
    """[(camera name, label or None)] from "name" or "name | Label" lines."""
    wanted = []
    for line in lines or []:
        name, _, label = (part.strip() for part in str(line).partition("|"))
        if name:
            wanted.append((name, label or None))
    return wanted


def parse_frigate_config(config, wanted):
    """Cameras from Frigate's /api/config: name, label, picture size and the go2rtc stream for live video.

    `wanted` limits and orders the result ("name" or "name | Label" lines); empty means every
    enabled camera. `stream` is None when Frigate has no restream for a camera, which then shows pictures.
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
        cameras[name] = {"name": name, "label": name.replace("_", " "), "stream": stream,
                         "width": detect.get("width") or 0, "height": detect.get("height") or 0}
    chosen = parse_camera_lines(wanted)
    if chosen:
        lookup = {name.lower(): cam for name, cam in cameras.items()}
        picked = []
        for name, label in chosen:
            if name.lower() in lookup:
                picked.append({**lookup[name.lower()], "label": label or lookup[name.lower()]["label"]})
        return picked
    return list(cameras.values())


def frigate_cameras(base):
    """Every camera Frigate knows, for the picker in the module form. None if Frigate cannot be reached."""
    base = (base or "").rstrip("/")
    if not base:
        return None
    try:
        body, _ = _get(f"{base}/api/config")
        return parse_frigate_config(json.loads(body), [])
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None


def fetch_frigate(cfg, content_id):
    base = (cfg.get("frigate") or "").rstrip("/")
    if not base:
        raise ServiceError("No Frigate address: set one on the Settings page or in this module")
    body, _ = _get(f"{base}/api/config")
    return {"cameras": parse_frigate_config(json.loads(body), cfg.get("cameras"))}


# --- service status -----------------------------------------------------------------

MAX_SERVICES = 40
_since = {}     # (content id, service name) -> (up, when it last changed)


_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:_-]{0,252}")


def ping(host, timeout=5):
    """One ICMP ping with the system's ping command: (up, milliseconds or None, reason when down)."""
    if not _HOST.fullmatch(host):       # also keeps anything that looks like an option away from ping
        return False, None, "bad address"
    if not shutil.which("ping"):
        return False, None, "ping is not installed on the server"
    wait = max(1, round(timeout))
    command = ["ping", "-n", "1", "-w", str(wait * 1000), host] if os.name == "nt" else \
              ["ping", "-c", "1", "-W", str(wait), host]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=wait + 3)
    except (OSError, subprocess.SubprocessError):
        return False, None, "no answer"
    took = re.search(r"time[=<]\s*([\d.]+)\s*ms", result.stdout)
    if result.returncode != 0 or not took:
        unknown = re.search(r"not known|could not find host|unknown host|failure in name", result.stdout + result.stderr, re.I)
        return False, None, "name not found" if unknown else "no answer"
    return True, round(float(took.group(1))), ""


def check_service(target, timeout=5, auth_ok=True, insecure=True):
    """Whether one service answers: (up, milliseconds or None, short reason when down).

    http(s):// addresses are fetched; tcp://host:port only has to accept a connection;
    ping://host, or just a host name or IP address, is pinged.
    """
    if target.startswith("ping://"):
        return ping(target[len("ping://"):].strip("/"), timeout)
    if "://" not in target:
        return ping(target, timeout)
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
        services.append((name or urlsplit(target if "://" in target else "//" + target).hostname or target, target))
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


# --- photo frame ----------------------------------------------------------------------

def fetch_photos(cfg, content_id):
    from . import photos
    album = " ".join((cfg.get("album") or "").split())
    folder = photos.album_dir(album)
    if folder is None or not folder.is_dir():
        raise ServiceError(f"There is no album called {album or '(blank)'}. Create it under Photos.")
    return {"photos": photos.list_photos(album)}


# --- board: shows whatever was last pushed to it ----------------------------------------

BOARD_STATES = ("ok", "warn", "bad")
BOARD_MAX_ITEMS = 40


def _short(value, limit):
    """A value from a pushed payload as display text, or None when it was not given."""
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)[:limit]


def clean_board_payload(body):
    """Keep only the fields a board shows, in the shapes it expects."""
    if not isinstance(body, dict):
        raise ServiceError("Send a JSON object")
    payload = {}
    for key, limit in (("title", 80), ("value", 40), ("label", 80), ("text", 400)):
        text = _short(body.get(key), limit)
        if text is not None and text != "":
            payload[key] = text
    if body.get("status") in BOARD_STATES:
        payload["status"] = body["status"]
    items = body.get("items")
    if items is not None:
        if not isinstance(items, list):
            raise ServiceError("items must be a list")
        payload["items"] = []
        for item in items[:BOARD_MAX_ITEMS]:
            if isinstance(item, dict):
                row = {"label": _short(item.get("label"), 120) or ""}
                if _short(item.get("value"), 40) is not None:
                    row["value"] = _short(item.get("value"), 40)
                if item.get("status") in BOARD_STATES:
                    row["status"] = item["status"]
            else:
                row = {"label": _short(item, 120) or ""}
            if row["label"] or "value" in row:
                payload["items"].append(row)
    return payload


def push_board(db, ref, body):
    """Store what a board should show. `ref` is the board's content id or its name."""
    row = db.execute("SELECT id FROM content_items WHERE kind = 'module' AND module = 'board'"
                     " AND (id = ? OR name = ?)", (ref if str(ref).isdigit() else -1, str(ref))).fetchone()
    if row is None:
        raise ServiceError(f"No board called {ref}", 404)
    payload = clean_board_payload(body)
    db.execute("INSERT INTO board_data (content_id, payload, updated_at) VALUES (?, ?, ?)"
               " ON CONFLICT(content_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at",
               (row["id"], json.dumps(payload), time.time()))
    db.commit()
    with _cache_lock:       # show it on the next poll, not when the cache runs out
        for key in [k for k in _cache if k[0] == row["id"]]:
            del _cache[key]
    return payload


def fetch_board(cfg, content_id):
    row = get_db().execute("SELECT payload, updated_at FROM board_data WHERE content_id = ?", (content_id,)).fetchone()
    if row is None:
        return {"payload": None, "updated": None}
    stale = float(cfg.get("stale_minutes") or 0) * 60
    if stale and time.time() - row["updated_at"] > stale:
        return {"payload": None, "updated": round(row["updated_at"])}
    return {"payload": json.loads(row["payload"]), "updated": round(row["updated_at"])}


# --- sun, air quality (Open-Meteo) --------------------------------------------------------

def _place(cfg):
    location = (cfg.get("location") or "").strip()
    if not location:
        raise ServiceError("No location configured")
    return _geocode(location)


def fetch_sun(cfg, content_id):
    latitude, longitude, place = _place(cfg)
    body, _ = _get("https://api.open-meteo.com/v1/forecast?" + urlencode({
        "latitude": latitude, "longitude": longitude, "timezone": "auto", "forecast_days": 2,
        "daily": "sunrise,sunset,daylight_duration"}))
    daily = json.loads(body)["daily"]
    return {"place": place, "days": [
        {"date": daily["time"][i], "sunrise": daily["sunrise"][i], "sunset": daily["sunset"][i],
         "daylight_s": round(daily["daylight_duration"][i])} for i in range(len(daily["time"]))]}


POLLEN_KINDS = ("alder", "birch", "grass", "mugwort", "olive", "ragweed")


def fetch_air(cfg, content_id):
    latitude, longitude, place = _place(cfg)
    fields = ["european_aqi", "pm10", "pm2_5", "uv_index"] + [f"{kind}_pollen" for kind in POLLEN_KINDS]
    body, _ = _get("https://air-quality-api.open-meteo.com/v1/air-quality?" + urlencode({
        "latitude": latitude, "longitude": longitude, "timezone": "auto", "current": ",".join(fields)}))
    current = json.loads(body)["current"]
    return {"place": place, "aqi": current.get("european_aqi"), "pm2_5": current.get("pm2_5"),
            "pm10": current.get("pm10"), "uv": current.get("uv_index"),
            "pollen": {kind: current[f"{kind}_pollen"] for kind in POLLEN_KINDS
                       if current.get(f"{kind}_pollen") is not None}}


# --- QR code ---------------------------------------------------------------------------

def fetch_qr(cfg, content_id):
    import segno
    from segno import helpers
    if cfg.get("kind") == "wifi":
        ssid = (cfg.get("ssid") or "").strip()
        if not ssid:
            raise ServiceError("No Wi-Fi name set")
        security = cfg.get("security") if cfg.get("security") in ("WPA", "WEP") else None
        code = helpers.make_wifi(ssid=ssid, password=cfg.get("password") or None if security else None, security=security)
    else:
        content = (cfg.get("content") or "").strip()
        if not content:
            raise ServiceError("Nothing to put in the code")
        try:
            code = segno.make(content, error="m")
        except (ValueError, segno.DataOverflowError):
            raise ServiceError("That is too long for a QR code")
    # A standalone SVG (with its namespace): the page shows it as an image, not inline markup.
    out = io.BytesIO()
    code.save(out, kind="svg", scale=10, border=3, dark="#000", light="#fff", xmldecl=False, svgns=True)
    return {"svg": out.getvalue().decode("utf-8")}


# --- flights overhead (adsb.lol / airplanes.live, routes from adsb.lol, photos from planespotters.net) ---

FLIGHT_SOURCES = (
    ("adsb.lol", "https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{nm}"),
    ("airplanes.live", "https://api.airplanes.live/v2/point/{lat:.4f}/{lon:.4f}/{nm}"),
)
ROUTESET_URL = "https://api.adsb.lol/api/0/routeset"
PHOTO_URL = "https://api.planespotters.net/pub/photos/hex/{hex}"
# Planespotters refuses clients whose User-Agent has no contact address.
FLIGHTS_USER_AGENT = "VitalSigns/1.0 (+https://github.com/marctew/vital-signs)"
FLIGHT_DATA = os.path.join(os.path.dirname(__file__), "flightdata")
MAX_AIRCRAFT = 150
ROUTE_TTL, ROUTE_MISS_TTL, ROUTES_PER_FETCH = 6 * 3600, 1800, 30
PHOTO_TTL, PHOTO_MISS_TTL, PHOTOS_PER_FETCH = 24 * 3600, 6 * 3600, 3
TRAIL_POINTS, TRAIL_FORGET = 40, 300
LOOKUP_BACKOFF = 60              # after a route or photo service fails, leave it alone this long
EMERGENCY_SQUAWKS = ("7500", "7600", "7700")
_COORDS = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_AIRLINE_CALLSIGN = re.compile(r"^([A-Z]{3})\d")

_flight_lock = threading.Lock()
_routes = {}         # callsign -> (time, route or None)
_photos = {}         # hex -> (time, photo or None)
_trails = {}         # content id -> {hex: [last seen, [(lat, lon), ...]]}
_lookup_down = {}    # "routes" / "photos" -> time of the last failure
_tables = {}


def _flight_table(name):
    """airlines.json / aircraft_types.json from server/flightdata (built by tools/make_flight_data.py)."""
    if name not in _tables:
        try:
            with open(os.path.join(FLIGHT_DATA, name), encoding="utf-8") as f:
                _tables[name] = json.load(f)
        except (OSError, ValueError):
            log.warning("flights: could not read %s", name)
            _tables[name] = {}
    return _tables[name]


def _centre(cfg):
    """(lat, lon, place) from a "lat, lon" pair or a place name."""
    location = (cfg.get("location") or "").strip()
    if not location:
        raise ServiceError("No location configured")
    match = _COORDS.match(location)
    if match:
        lat, lon = float(match.group(1)), float(match.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ServiceError("That latitude and longitude is not on the map")
        return lat, lon, f"{lat:.3f}, {lon:.3f}"
    return _geocode(location)


def _distance_bearing(lat1, lon1, lat2, lon2):
    """Great-circle distance in km and initial bearing in degrees from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    km = 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return km, (math.degrees(math.atan2(y, x)) + 360) % 360


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _icon_kind(category, cls, wtc):
    """Which silhouette to draw: light, jet, heavy, heli or other."""
    category, cls = category or "", cls or ""
    if category == "A7" or cls.startswith(("H", "G")):
        return "heli"
    if category[:1] in ("B", "C"):
        return "other"
    if category == "A5" or wtc in ("H", "J"):
        return "heavy"
    if category in ("A3", "A4", "A6") or cls.endswith("J"):
        return "jet"
    if category in ("A1", "A2") or cls[:1] == "L":
        return "light"
    return "jet" if wtc == "M" else "light"


def normalise_aircraft(raw, lat, lon):
    """One aircraft from a readsb-format "ac" entry, or None if it has no recent position."""
    if _number(raw.get("lat")) is None or _number(raw.get("lon")) is None:
        return None
    if (_number(raw.get("seen_pos")) or 0) > 60:
        return None
    hex_ = str(raw.get("hex") or "").lower()
    alt_baro = raw.get("alt_baro")
    ground = alt_baro == "ground"
    alt = 0 if ground else _number(alt_baro)
    if alt is None:
        alt = _number(raw.get("alt_geom"))
    track = _number(raw.get("track"))
    if track is None:
        track = _number(raw.get("true_heading"))
    vrate = _number(raw.get("baro_rate"))
    if vrate is None:
        vrate = _number(raw.get("geom_rate"))
    callsign = str(raw.get("flight") or "").strip()
    squawk = str(raw.get("squawk") or "")
    type_code = str(raw.get("t") or "").upper()
    type_name, cls, wtc = (_flight_table("aircraft_types.json").get(type_code) or ["", "", ""])
    if raw.get("desc"):
        type_name = raw["desc"]
    match = _AIRLINE_CALLSIGN.match(callsign)
    airline = None
    if match:
        found = _flight_table("airlines.json").get(match.group(1))
        if found:
            airline = {"code": match.group(1), "name": found[0], "iata": found[1]}
    km, bearing = _distance_bearing(lat, lon, raw["lat"], raw["lon"])
    emergency = str(raw.get("emergency") or "none") not in ("none", "") or squawk in EMERGENCY_SQUAWKS
    return {
        "hex": hex_, "callsign": callsign, "reg": str(raw.get("r") or ""), "type": type_code,
        "type_name": type_name, "operator": str(raw.get("ownOp") or ""), "airline": airline,
        "lat": round(raw["lat"], 4), "lon": round(raw["lon"], 4),
        "alt": None if alt is None else int(alt), "ground": ground,
        "gs": None if _number(raw.get("gs")) is None else round(raw["gs"]),
        "track": None if track is None else round(track),
        "vrate": None if vrate is None else int(vrate),
        "squawk": squawk, "category": str(raw.get("category") or ""),
        "icon": _icon_kind(raw.get("category"), cls, wtc),
        "military": bool((_number(raw.get("dbFlags")) or 0) & 1), "emergency": emergency,
        "dist_km": round(km, 1), "bearing": round(bearing),
    }


def filter_aircraft(aircraft, cfg):
    """Apply the range, ground and altitude options; nearest first, capped at MAX_AIRCRAFT."""
    range_km = float(cfg.get("range_km") or 40)
    min_alt, max_alt = float(cfg.get("min_alt") or 0), float(cfg.get("max_alt") or 0)
    kept = []
    for a in aircraft:
        if a is None or a["dist_km"] > range_km:
            continue
        if a["ground"] and cfg.get("hide_ground", True):
            continue
        if min_alt and (a["alt"] is None or a["alt"] < min_alt) and not a["ground"]:
            continue
        if max_alt and a["alt"] is not None and a["alt"] > max_alt:
            continue
        kept.append(a)
    kept.sort(key=lambda a: a["dist_km"])
    return kept[:MAX_AIRCRAFT]


def _flight_get(url):
    return _get(url, {"User-Agent": FLIGHTS_USER_AGENT})


def _flight_post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
        "User-Agent": FLIGHTS_USER_AGENT, "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = r.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("response too large")
    return json.loads(body)          # an empty reply is a ValueError: the service is not working


def _flight_aircraft(cfg, lat, lon):
    """(source name, list of raw readsb aircraft) from the first public API that answers.

    A local receiver (readsb / tar1090 aircraft.json) would slot in here."""
    nm = max(1, min(250, math.ceil(float(cfg.get("range_km") or 40) / 1.852)))
    error = None
    for name, template in FLIGHT_SOURCES:
        try:
            body, _ = _flight_get(template.format(lat=lat, lon=lon, nm=nm))
            payload = json.loads(body)
            if not isinstance(payload.get("ac"), list):
                raise ValueError(f"{name} reply has no aircraft list")
            return name, payload["ac"]
        except (urllib.error.URLError, OSError, ValueError, AttributeError) as e:
            log.info("flights: %s failed: %r", name, e)
            error = e
    raise error if isinstance(error, (urllib.error.URLError, OSError)) else ValueError(str(error))


def _backing_off(kind, now):
    return now - _lookup_down.get(kind, 0) < LOOKUP_BACKOFF


def _parse_route(entry):
    if not isinstance(entry, dict) or not entry.get("plausible"):
        return None
    airports = [a for a in entry.get("_airports") or [] if isinstance(a, dict)]
    if len(airports) < 2:
        return None

    def place(a):
        return {"iata": str(a.get("iata") or ""), "icao": str(a.get("icao") or ""),
                "name": str(a.get("name") or ""), "city": str(a.get("location") or ""),
                "country": str(a.get("countryiso2") or "")}
    return {"from": place(airports[0]), "to": place(airports[-1]),
            "via": [place(a) for a in airports[1:-1]]}


def _lookup_routes(aircraft, now):
    """Fill in a["route"] from the cache, asking adsb.lol for up to ROUTES_PER_FETCH new callsigns."""
    wanted = []
    for a in aircraft:
        callsign = a["callsign"]
        if not _AIRLINE_CALLSIGN.match(callsign):
            continue
        hit = _routes.get(callsign)
        if hit and now - hit[0] < (ROUTE_TTL if hit[1] else ROUTE_MISS_TTL):
            continue
        if callsign not in {w["callsign"] for w in wanted}:
            wanted.append({"callsign": callsign, "lat": a["lat"], "lng": a["lon"]})
    wanted = wanted[:ROUTES_PER_FETCH]
    if wanted and not _backing_off("routes", now):
        try:
            reply = _flight_post(ROUTESET_URL, {"planes": wanted})
            if not isinstance(reply, list):
                raise ValueError("route reply is not a list")
            found = {str(e.get("callsign") or "").strip(): _parse_route(e) for e in reply if isinstance(e, dict)}
            for w in wanted:
                _routes[w["callsign"]] = (now, found.get(w["callsign"]))
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.debug("flights: route lookup failed: %r", e)
            _lookup_down["routes"] = now
    for a in aircraft:
        hit = _routes.get(a["callsign"])
        a["route"] = hit[1] if hit else None


def _lookup_photos(aircraft, now):
    """Fill in a["photo"] from the cache, asking planespotters.net for a few new ones, nearest first."""
    asked = 0
    for a in aircraft[:10]:
        if asked >= PHOTOS_PER_FETCH or _backing_off("photos", now):
            break
        hex_ = a["hex"]
        if not re.fullmatch(r"[0-9a-f]{6}", hex_):
            continue
        hit = _photos.get(hex_)
        if hit and now - hit[0] < (PHOTO_TTL if hit[1] else PHOTO_MISS_TTL):
            continue
        asked += 1
        try:
            body, _ = _flight_get(PHOTO_URL.format(hex=hex_))
            photos = json.loads(body).get("photos") or []
            photo = None
            for p in photos:
                thumb = p.get("thumbnail_large") or p.get("thumbnail") or {}
                src = str(thumb.get("src") or "")
                if src.startswith("https://"):
                    photo = {"src": src, "link": str(p.get("link") or ""),
                             "photographer": str(p.get("photographer") or "")}
                    break
            _photos[hex_] = (now, photo)
        except (urllib.error.URLError, OSError, ValueError, AttributeError) as e:
            log.debug("flights: photo lookup failed: %r", e)
            _lookup_down["photos"] = now
    for a in aircraft:
        hit = _photos.get(a["hex"])
        a["photo"] = hit[1] if hit else None


def _update_trails(content_id, aircraft, now):
    trails = _trails.setdefault(content_id, {})
    for a in aircraft:
        seen = trails.setdefault(a["hex"], [now, []])
        seen[0] = now
        point = (a["lat"], a["lon"])
        if not seen[1] or seen[1][-1] != point:
            seen[1].append(point)
            del seen[1][:-TRAIL_POINTS]
        a["trail"] = [list(p) for p in seen[1]]
    for hex_ in [h for h, (last, _) in trails.items() if now - last > TRAIL_FORGET]:
        del trails[hex_]


def fetch_flights(cfg, content_id):
    lat, lon, place = _centre(cfg)
    source, raw = _flight_aircraft(cfg, lat, lon)
    aircraft = filter_aircraft([normalise_aircraft(r, lat, lon) for r in raw if isinstance(r, dict)], cfg)
    now = time.time()
    with _flight_lock:
        if cfg.get("route", True):
            _lookup_routes(aircraft, now)
        if cfg.get("photo", True):
            _lookup_photos(aircraft, now)
        if cfg.get("trails", True):
            _update_trails(content_id, aircraft, now)
    for a in aircraft:
        a.setdefault("route", None)
        a.setdefault("photo", None)
        a.setdefault("trail", [])
    return {"centre": {"lat": lat, "lon": lon, "place": place}, "range_km": float(cfg.get("range_km") or 40),
            "source": source, "fetched": round(now), "aircraft": aircraft}


# --- 3D printers (Bambu Lab, through Home Assistant) ---------------------------
#
# The ha-bambulab integration names every entity of a printer <prefix>_<thing>, the prefix being
# the model and serial (p1s_01p09c4a1401920). A printer is any prefix with both a print_status
# and a print_progress sensor. One template call asks Home Assistant for just those entities;
# a token without admin rights may not render templates, so then the whole state list is read.

HA_PRINTER_TEMPLATE = """
{%- set ns = namespace(found=[], out=[]) -%}
{%- set progress = states.sensor | map(attribute='object_id') | select('search', '_print_progress$') | list -%}
{%- for s in states.sensor if s.object_id.endswith('_print_status') -%}
{%- set p = s.object_id[:-13] -%}
{%- if (not wanted or p in wanted) and (p ~ '_print_progress') in progress -%}{%- set ns.found = ns.found + [p] -%}{%- endif -%}
{%- endfor -%}
{%- for s in states if s.domain in ('sensor', 'binary_sensor', 'camera', 'image') -%}
{%- for p in ns.found if s.object_id.startswith(p ~ '_') -%}
{%- set a = s.attributes -%}
{%- set ns.out = ns.out + [{'entity_id': s.entity_id, 'state': s.state, 'attributes': {
     'unit_of_measurement': a.get('unit_of_measurement'), 'color': a.get('color'), 'type': a.get('type'),
     'active': a.get('active'), 'empty': a.get('empty')}}] -%}
{%- endfor -%}{%- endfor -%}
{{ ns.out | to_json }}
"""
HA_STATES_MAX_BYTES = 40 * 1024 * 1024
PRINTER_ACTIVE = ("running", "pause", "prepare", "slicing", "init", "failed")
PRINTER_IMAGES = {"camera": "/api/camera_proxy/camera.{}_camera", "cover": "/api/image_proxy/image.{}_cover_image"}
_PRINTER_PREFIX = re.compile(r"[a-z0-9_]{1,120}")
_MISSING = ("unknown", "unavailable", "none", "")


def parse_printer_lines(lines):
    """[(prefix, label)] from 'prefix | Label' lines. A pasted entity id is cut down to its prefix."""
    printers = []
    for line in lines:
        prefix, _, label = (part.strip() for part in line.partition("|"))
        prefix = prefix.lower()
        if "." in prefix:
            prefix = prefix.split(".", 1)[1]
        for suffix in ("_print_status", "_print_progress"):
            if prefix.endswith(suffix):
                prefix = prefix[:-len(suffix)]
        if _PRINTER_PREFIX.fullmatch(prefix) and prefix not in dict(printers):
            printers.append((prefix, label))
    return printers


def _ha(cfg):
    base = (cfg.get("ha_url") or "").strip().rstrip("/")
    if not base:
        raise ServiceError("Set the Home Assistant address on the Settings page.")
    if not base.startswith(("http://", "https://")):
        raise ServiceError("The Home Assistant address must start with http:// or https://")
    if not cfg.get("ha_token"):
        raise ServiceError("Set a Home Assistant access token on the Settings page.")
    return base, {"Authorization": f"Bearer {cfg['ha_token']}", "User-Agent": "vitalsigns"}


def _ha_json(url, headers, payload=None, limit=MAX_BYTES):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={**headers, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = r.read(limit + 1)
    if len(body) > limit:
        raise ValueError("response too large")
    return json.loads(body)


def _ha_printer_states(cfg, wanted):
    base, headers = _ha(cfg)
    try:
        return _ha_json(f"{base}/api/template", headers,
                        {"template": HA_PRINTER_TEMPLATE, "variables": {"wanted": wanted}})
    except urllib.error.HTTPError as e:
        if e.code not in (400, 401, 403):
            raise
    try:
        return _ha_json(f"{base}/api/states", headers, limit=HA_STATES_MAX_BYTES)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ServiceError("Home Assistant did not accept the access token.") from e
        raise


def _colour(value):
    """'#RRGGBBAA' from Bambu to '#rrggbb', or None for no colour (empty or unknown, alpha 00)."""
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})([0-9a-fA-F]{2})?", str(value or ""))
    if not m or m.group(2) == "00":
        return None
    return "#" + m.group(1).lower()


def parse_printers(states, wanted=None):
    """Printer summaries from Home Assistant states (the /api/states shape), in the order asked for."""
    wanted = wanted or []
    by_id = {s.get("entity_id"): s for s in states if isinstance(s, dict)}
    found = sorted(e[7:-13] for e in by_id
                   if e and e.startswith("sensor.") and e.endswith("_print_status")
                   and f"{e[:-13]}_print_progress" in by_id)
    labels = dict(wanted)
    order = [p for p, _ in wanted] if wanted else found

    def entity(p, thing, domain="sensor"):
        return by_id.get(f"{domain}.{p}_{thing}")

    def text(p, thing, domain="sensor"):
        s = entity(p, thing, domain)
        value = str(s.get("state", "")) if s else ""
        return None if value.lower() in _MISSING else value

    def number(p, thing):
        try:
            value = float(text(p, thing))
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def flag(p, thing):
        value = text(p, thing, "binary_sensor")
        return None if value is None else value == "on"

    def spool(s, name):
        a = s.get("attributes") or {}
        empty = bool(a.get("empty")) or str(s.get("state", "")).lower() in ("empty",) + _MISSING
        return {"slot": name, "name": None if empty else s["state"], "type": None if empty else a.get("type"),
                "colour": None if empty else _colour(a.get("color")), "active": bool(a.get("active")), "empty": empty}

    printers = []
    for p in order:
        if p not in found:
            printers.append({"id": p, "name": labels.get(p) or p, "missing": True})
            continue
        status = (text(p, "print_status") or "unknown").lower()
        online = flag(p, "online")
        if online is False:
            status = "offline"
        remaining = number(p, "remaining_time")
        unit = ((entity(p, "remaining_time") or {}).get("attributes") or {}).get("unit_of_measurement")
        if remaining is not None:
            remaining = round(remaining * {"h": 60, "s": 1 / 60, "d": 1440}.get(unit, 1))
        temps = []
        for key, label in (("nozzle", "Nozzle"), ("bed", "Bed"), ("chamber", "Chamber")):
            now_t = number(p, f"{key}_temperature")
            if now_t is not None:
                temps.append({"name": label, "now": round(now_t), "target": round(number(p, f"{key}_target_temperature") or 0)})
        units = {}
        for eid, s in by_id.items():
            m = re.fullmatch(rf"sensor\.{re.escape(p)}_ams_(\d+)_tray_(\d+)", eid or "")
            if m:
                units.setdefault(int(m.group(1)), []).append((int(m.group(2)), spool(s, m.group(2))))
        filament = []
        for n in sorted(units):
            filament.append({"name": f"AMS {n}" if len(units) > 1 else "AMS",
                             "humidity": number(p, f"ams_{n}_humidity"),
                             "trays": [t for _, t in sorted(units[n], key=lambda x: x[0])]})
        external = entity(p, "externalspool_external_spool")
        if external:
            tray = spool(external, "Ext")
            if not tray["empty"] or not filament:
                filament.append({"name": "External", "humidity": None, "trays": [tray]})
        camera = entity(p, "camera", "camera")
        layers = number(p, "total_layer_count")
        printers.append({
            "id": p,
            "name": labels.get(p) or text(p, "printer_name") or p.split("_")[0].upper(),
            "model": p.split("_")[0].upper(),
            "status": status,
            "active": status in PRINTER_ACTIVE,
            "stage": text(p, "current_stage"),
            "progress": number(p, "print_progress"),
            "task": text(p, "task_name"),
            "layer": int(number(p, "current_layer") or 0) if layers else None,
            "layers": int(layers) if layers else None,
            "remaining_min": remaining,
            "end": text(p, "end_time"),
            "weight_g": number(p, "print_weight"),
            "temps": temps,
            "error": bool(flag(p, "print_error")) or bool(flag(p, "hms_errors")),
            "door_open": flag(p, "enclosure_door"),
            "filament": filament,
            "camera": bool(camera) and str(camera.get("state")).lower() not in _MISSING,
            "cover": entity(p, "cover_image", "image") is not None,
        })
    return printers


def fetch_printer(cfg, content_id):
    wanted = parse_printer_lines(cfg.get("printers") or [])
    states = _ha_printer_states(cfg, [p for p, _ in wanted])
    if not isinstance(states, list):
        raise ValueError("unexpected reply")
    printers = parse_printers(states, wanted)
    if not printers:
        raise ServiceError("Home Assistant has no Bambu Lab printers (no *_print_status sensors).")
    return {"printers": printers, "fetched": round(time.time())}


def printer_choices(stored):
    """[{id, name}] for every printer Home Assistant has, for the dropdown in the module form.

    Uses the item's own address and token, else the Settings ones. None if Home Assistant cannot be asked.
    """
    cfg = {"ha_url": stored.get("ha_url") or setting("ha_url"),
           "ha_token": stored.get("ha_token") or setting("ha_token")}
    try:
        printers = parse_printers(_ha_printer_states(cfg, []))
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, ServiceError):
        return None
    return [{"id": p["id"], "name": p["name"], "model": p.get("model", "")} for p in printers]


def printer_image(cfg):
    """A camera still or the model picture for one printer, fetched from Home Assistant with the stored token."""
    prefix, kind = request.args.get("printer", ""), request.args.get("kind", "")
    if not _PRINTER_PREFIX.fullmatch(prefix) or kind not in PRINTER_IMAGES:
        return jsonify(error="not found"), 404
    wanted = [p for p, _ in parse_printer_lines(cfg.get("printers") or [])]
    if wanted and prefix not in wanted:
        return jsonify(error="not found"), 404
    try:
        base, headers = _ha(cfg)
        url = base + PRINTER_IMAGES[kind].format(prefix)
        width = request.args.get("w", type=int)
        if kind == "camera" and width and 160 <= width <= 1920:
            url += "?" + urlencode({"width": width})
        body, mimetype = _get(url, headers)
    except (urllib.error.URLError, OSError, ValueError, ServiceError):
        return jsonify(error="not available"), 502
    if not mimetype.startswith("image/"):
        return jsonify(error="not an image"), 502
    resp = Response(body, mimetype=mimetype)
    resp.headers["Cache-Control"] = "no-store" if kind == "camera" else "max-age=60"
    return resp


# data type in module.json -> (fetch function, cache seconds)
PROVIDERS = {
    "rss": (fetch_rss, 300),
    "calendar": (fetch_calendar, 300),
    "plex": (fetch_plex, 8),
    "weather": (fetch_weather, 900),
    "frigate": (fetch_frigate, 300),
    "status": (fetch_status, 25),
    "photos": (fetch_photos, 30),
    "board": (fetch_board, 5),
    "sun": (fetch_sun, 3600),
    "air": (fetch_air, 1800),
    "qr": (fetch_qr, 3600),
    "flights": (fetch_flights, 5),
    "printer": (fetch_printer, 5),
}
