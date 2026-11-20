"""E-ink displays (TRMNL): what each one should show, when it should wake, and the pictures it is sent.

A TRMNL cannot run a browser. It wakes, asks `/api/display` (server/device_api.py) what to draw,
downloads one PNG, draws it and sleeps for as long as it was told. So the server does three things
the Pi agent does for itself: it decides which playlist item is current, it renders that page to a
picture in the panel's inks, and it works out when the device next needs to look.

Design and the reasons for it: docs/specs/trmnl-eink.md.
"""
import hashlib
import hmac
import io
import json
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import db as database
from . import services
from .services import ServiceError

log = logging.getLogger("server.eink")

# Inks per palette, in the order they are indexed in the PNGs we send.
PALETTES = {
    "bwry": [(0, 0, 0), (255, 255, 255), (255, 0, 0), (255, 255, 0)],
    "bw": [(0, 0, 0), (255, 255, 255)],
}
# Roughly how the inks look on the panel, for matching photos: the red is darker and the yellow duller
# than pure red and yellow. Estimates, not measurements of any one panel. The PNG still holds the pure inks.
INK_LOOKS = {
    "bwry": [(0, 0, 0), (255, 255, 255), (205, 35, 30), (245, 210, 20)],
    "bw": [(0, 0, 0), (255, 255, 255)],
}
PHOTO_CONTRAST_CUTOFF = 0.5  # percent of the darkest and lightest pixels let go when stretching the tones
PHOTO_GREY_BELOW = 40       # saturation (0 to 255) under which a colour is treated as grey: a warm-grey sofa or a
PHOTO_COLOUR_FROM = 110     # pale wall is made of black and white, not sprinkled with red. Full colour from here up,
PHOTO_SATURATION = 1.3      # pushed a little, because e-ink mutes colour
PHOTO_GAMMA = 0.9           # and lightened: the panel's white is a pale grey
PHOTO_CHROMA_WEIGHT = 2.0   # how much more a colour mismatch counts than a brightness one when choosing an ink
PHOTO_ERROR_KEPT = 0.75     # Atkinson: a quarter of the error is dropped, which keeps highlights and shadows clean
# The firmware names its hardware in the Model header. Anything unknown is treated as black and white.
MODELS = {"og_4clr": "bwry", "og_gen2_4clr": "bwry", "og": "bw", "og_gen2": "bw"}
MODEL_NAMES = {"og_4clr": "TRMNL BWRY", "og_gen2_4clr": "TRMNL BWRY (gen 2)", "og": "TRMNL", "og_gen2": "TRMNL (gen 2)"}
BLACK, WHITE, RED = (0, 0, 0), (255, 255, 255), (255, 0, 0)

FOOTER_PX = 28
DEFAULT_SIZE = (800, 480)
RENDER_MARGIN = 20          # seconds after a re-render is due before the device is asked to wake for it
PENDING_RATE = 20           # a picture is being drawn right now: come straight back
ADOPT_RATE = 60             # not adopted yet: look again in a minute
RETRY_FAILED = 120          # seconds before a failed render is tried again
OFF_HOURS_CAP = 12 * 3600   # outside wake hours it still checks in this often
KEEP_FILES = 20
PAIR_MINUTES = 10
READING_GAP = 120           # seconds between kept battery readings: a burst of button presses is one reading
READINGS_KEPT_DAYS = 400
CHART_POINTS = 240          # the most points a battery chart is sent; longer ranges are averaged into this many
ESTIMATE_MIN_HOURS = 24     # how much of a discharge has to be on record before a battery life estimate is offered
ESTIMATE_MIN_DROP = 2.0     # and how many percentage points it has to have fallen
CHARGE_JUMP = 5.0           # a rise of this many points between readings means it was charged: a new discharge starts
MAX_INSET = 120             # pixels a frame may cover on one side
MAX_MIN_TEXT = 40           # the largest "smallest text" a display may ask for, in pixels
TEXT_INK_GAMMA = 1.6        # flat-colour pages: how far the soft grey edges of text are pushed towards black, so thin
                            # strokes survive being cut to pure ink instead of breaking up (1 would cut at mid-grey)
SIDES = ("top", "right", "bottom", "left")
LOG_ROWS = 200
READY_TIMEOUT = 20
SHOW_PLAYLIST = "Playlist"  # the Home Assistant "Show" option that means "no override"

_MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


# --- small helpers --------------------------------------------------------------

def palette_name(model):
    return MODELS.get(model or "", "bw")


def model_name(model):
    return MODEL_NAMES.get(model or "", model or "E-ink display")


def battery_percent(volts):
    """A rough charge from a lithium cell's voltage: 4.1 V and above is full, 3.0 V is empty."""
    try:
        volts = float(volts)
    except (TypeError, ValueError):
        return None
    if volts <= 0.5:        # no reading (the firmware sends 0 when it could not measure)
        return None
    return max(0, min(100, round((volts - 3.0) / 1.1 * 100)))


def wifi_word(rssi):
    if not isinstance(rssi, (int, float)) or rssi >= 0:
        return ""
    return "good" if rssi >= -67 else ("OK" if rssi >= -78 else "weak")


def insets(dev):
    """Pixels of the panel hidden behind its frame: (top, right, bottom, left). Nothing is drawn there."""
    return tuple(int(dev[f"inset_{side}"] or 0) for side in SIDES)


def clock(when):
    return datetime.fromtimestamp(when).strftime("%H:%M")


def image_dir(data_dir, display_id):
    folder = Path(data_dir) / "eink" / str(display_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def image_path(data_dir, display_id, filename):
    """The stored picture of that name, or None. Names are hex digests, so nothing else can be asked for."""
    if not re.fullmatch(r"[0-9a-f]{8,64}", filename or ""):
        return None
    path = Path(data_dir) / "eink" / str(display_id) / f"{filename}.png"
    return path if path.is_file() else None


# --- devices --------------------------------------------------------------------

def clean_mac(value):
    mac = (value or "").strip().upper().replace("-", ":")
    return mac if _MAC.match(mac) else None


def get_device(db, mac):
    return db.execute("SELECT * FROM eink_devices WHERE mac = ?", (mac,)).fetchone()


def device_for_display(db, display_id):
    return db.execute(
        "SELECT e.* FROM eink_devices e JOIN displays d ON d.agent_id = e.agent_id WHERE d.id = ? AND d.kind = 'eink'",
        (display_id,)).fetchone()


def _size(headers):
    try:
        width, height = int(headers.get("Width") or 0), int(headers.get("Height") or 0)
    except ValueError:
        return 0, 0
    return (width, height) if 0 < width <= 4000 and 0 < height <= 4000 else (0, 0)


def register(db, mac, headers, now, api_key=None):
    """Remember a device seen for the first time. It stays pending until someone adopts it.

    `api_key` is the key the device presented, when it already has one (it was set up against
    another server, or forgotten here): keeping it means adoption needs nothing done on the device.
    """
    width, height = _size(headers)
    friendly = mac.replace(":", "")[-6:]
    db.execute(
        "INSERT INTO eink_devices (mac, api_key, friendly_id, model, fw_version, width, height, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (mac, api_key or secrets.token_urlsafe(24), friendly, (headers.get("Model") or "")[:40],
         (headers.get("FW-Version") or "")[:40], width, height, now, now))
    db.commit()
    log.info("New e-ink device %s (%s); adopt it on the dashboard", friendly, headers.get("Model") or "unknown model")
    return get_device(db, mac)


def check_key(dev, sent):
    return bool(sent) and hmac.compare_digest(dev["api_key"].encode(), sent.encode())


def setup_reply(db, mac, headers, now):
    """Answer GET /api/setup: (body, http status). The key is only given to a device that is not adopted
    yet, or within a few minutes of Re-pair being pressed, so knowing a MAC is not enough to get it."""
    dev = get_device(db, mac) or register(db, mac, headers, now)
    if dev["agent_id"] is not None and dev["pair_until"] < now:
        return {"status": 404, "api_key": None, "friendly_id": None, "image_url": None,
                "message": "Already adopted. Press Re-pair on this display's page in Vital Signs."}, 404
    db.execute("UPDATE eink_devices SET last_seen = ?, pair_until = 0 WHERE id = ?", (now, dev["id"]))
    db.commit()
    return {"status": 200, "api_key": dev["api_key"], "friendly_id": dev["friendly_id"], "image_url": None,
            "message": "Registered with Vital Signs"}, 200


def pending_devices(db):
    rows = db.execute("SELECT * FROM eink_devices WHERE agent_id IS NULL ORDER BY first_seen").fetchall()
    return [{"id": r["id"], "friendly_id": r["friendly_id"], "model": model_name(r["model"]),
             "size": f"{r['width']}×{r['height']}" if r["width"] else "", "last_seen": r["last_seen"]} for r in rows]


def adopt(db, device_id):
    """Make a pending device a display. Returns the display id."""
    dev = db.execute("SELECT * FROM eink_devices WHERE id = ?", (device_id,)).fetchone()
    if dev is None:
        raise ServiceError("No such device", 404)
    if dev["agent_id"] is not None:
        raise ServiceError("That device is already adopted")
    hostname, n = f"trmnl-{dev['friendly_id'].lower()}", 1
    while db.execute("SELECT 1 FROM agents WHERE hostname = ?", (hostname,)).fetchone():
        n += 1
        hostname = f"trmnl-{dev['friendly_id'].lower()}-{n}"
    width, height = (dev["width"], dev["height"]) if dev["width"] else DEFAULT_SIZE
    agent_id = db.execute("INSERT INTO agents (hostname, last_seen, git_sha) VALUES (?, ?, ?)",
                          (hostname, dev["last_seen"], dev["fw_version"])).lastrowid
    display_id = db.execute(
        "INSERT INTO displays (agent_id, connector, name, width, height, orientation, kind, transition)"
        " VALUES (?, 'eink', 'panel', ?, ?, ?, 'eink', 'none')",
        (agent_id, width, height, "portrait" if height > width else "landscape")).lastrowid
    db.execute("UPDATE eink_devices SET agent_id = ? WHERE id = ?", (agent_id, device_id))
    db.commit()
    return display_id


def forget_pending(db, device_id):
    db.execute("DELETE FROM eink_devices WHERE id = ? AND agent_id IS NULL", (device_id,))
    db.commit()


def rename(db, display_id, name):
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}", name):
        raise ServiceError("Use up to 40 letters, digits, spaces, dots, dashes or underscores for the name")
    dev = device_for_display(db, display_id)
    if dev is None:
        raise ServiceError("Not an e-ink display", 404)
    db.execute("UPDATE displays SET name = ? WHERE id = ?", (name, display_id))
    db.commit()


def allow_pairing(db, display_id):
    """Let the device fetch its key from /api/setup again, for when its settings were wiped."""
    dev = device_for_display(db, display_id)
    if dev is None:
        raise ServiceError("Not an e-ink display", 404)
    db.execute("UPDATE eink_devices SET pair_until = ? WHERE id = ?", (time.time() + PAIR_MINUTES * 60, dev["id"]))
    db.commit()


def save_settings(db, display_id, form):
    dev = device_for_display(db, display_id)
    if dev is None:
        raise ServiceError("Not an e-ink display", 404)

    def minutes(key, default, low, high):
        try:
            value = float(form.get(key) or default)
        except (TypeError, ValueError):
            raise ServiceError("Intervals must be numbers of minutes")
        if not low <= value <= high:
            raise ServiceError(f"Intervals must be between {low:g} and {high:g} minutes")
        return int(round(value * 60))

    # A field the form did not send keeps its value: a page left open from before a setting existed
    # must not reset that setting when it is saved.
    hidden = {}
    for side in SIDES:
        try:
            hidden[side] = dev[f"inset_{side}"] if form.get(f"inset_{side}") is None else int(float(form.get(f"inset_{side}") or 0))
        except (TypeError, ValueError):
            raise ServiceError("The frame margins must be whole numbers of pixels")
        if not 0 <= hidden[side] <= MAX_INSET:
            raise ServiceError(f"A frame margin must be between 0 and {MAX_INSET} pixels")
    try:
        min_text = dev["min_text_px"] if form.get("min_text_px") is None else int(float(form.get("min_text_px") or 0))
    except (TypeError, ValueError):
        raise ServiceError("The smallest text must be a whole number of pixels")
    if not 0 <= min_text <= MAX_MIN_TEXT:
        raise ServiceError(f"The smallest text must be between 0 and {MAX_MIN_TEXT} pixels")
    low = minutes("min_minutes", dev["min_interval_s"] / 60, 1, 720)
    high = minutes("max_minutes", dev["max_interval_s"] / 60, 1, 1440)
    if high < low:
        raise ServiceError("The longest sleep cannot be shorter than the shortest")
    db.execute("UPDATE eink_devices SET mains = ?, min_interval_s = ?, max_interval_s = ?, heartbeat_s = ?, footer = ?"
               " WHERE id = ?", (1 if form.get("mains") else 0, low, high,
                                 minutes("heartbeat_minutes", dev["heartbeat_s"] / 60, 5, 1440),
                                 1 if form.get("footer") else 0, dev["id"]))
    db.execute("UPDATE eink_devices SET inset_top = ?, inset_right = ?, inset_bottom = ?, inset_left = ?, min_text_px = ?"
               " WHERE id = ?", (*(hidden[side] for side in SIDES), min_text, dev["id"]))
    db.execute("UPDATE eink_renders SET checked_at = 0 WHERE display_id = ?", (display_id,))   # the footer may have changed
    db.commit()


def force_render(db, display_id):
    """Draw this display's pictures again now, and republish them even if nothing changed."""
    db.execute("UPDATE eink_renders SET checked_at = 0, published_at = 0, failed_at = 0 WHERE display_id = ?", (display_id,))
    db.commit()
    wake_renderer()


def touch_content(db, content_id):
    """A content item's data changed (a Board was pushed to): redraw it on the next pass."""
    db.execute("UPDATE eink_renders SET checked_at = 0, failed_at = 0 WHERE key = ?", (str(content_id),))
    db.commit()
    wake_renderer()


def record_wake(db, dev, headers, now):
    """Store what the device said about itself on this wake."""
    def number(name, cast=float):
        try:
            return cast(headers.get(name))
        except (TypeError, ValueError):
            return None

    telemetry = {
        "battery_v": number("Battery-Voltage"), "rssi": number("RSSI", int),
        "wake_reason": (headers.get("Update-Source") or "")[:40], "reset_reason": (headers.get("Reset-Reason") or "")[:40],
        "wake_ms": number("Wake-Time", int),
    }
    if headers.get("USB-Connected"):
        telemetry["usb"] = headers.get("USB-Connected") == "true"
    if headers.get("Battery-Charging"):
        telemetry["charging"] = headers.get("Battery-Charging") == "1"
    width, height = _size(headers)
    width, height = width or dev["width"], height or dev["height"]
    model = (headers.get("Model") or dev["model"])[:40]
    firmware = (headers.get("FW-Version") or dev["fw_version"])[:40]
    db.execute("UPDATE eink_devices SET last_seen = ?, telemetry = ?, model = ?, fw_version = ?, width = ?, height = ?"
               " WHERE id = ?", (now, json.dumps(telemetry), model, firmware, width, height, dev["id"]))
    volts = telemetry["battery_v"]
    if volts and volts > 0.5:       # the firmware sends 0 when it could not measure
        last = db.execute("SELECT MAX(at) FROM eink_readings WHERE device_id = ?", (dev["id"],)).fetchone()[0]
        if last is None or now - last >= READING_GAP:
            db.execute("INSERT INTO eink_readings (device_id, at, battery_v, rssi, wake_ms, wake_reason) VALUES (?, ?, ?, ?, ?, ?)",
                       (dev["id"], now, volts, telemetry["rssi"], telemetry["wake_ms"], telemetry["wake_reason"]))
            db.execute("DELETE FROM eink_readings WHERE device_id = ? AND at < ?", (dev["id"], now - READINGS_KEPT_DAYS * 86400))
    if dev["agent_id"] is not None:
        db.execute("UPDATE agents SET last_seen = ?, git_sha = ? WHERE id = ?", (now, firmware, dev["agent_id"]))
        if width:
            db.execute("UPDATE displays SET width = ?, height = ?, orientation = ? WHERE agent_id = ? AND kind = 'eink'",
                       (width, height, "portrait" if height > width else "landscape", dev["agent_id"]))


def add_logs(db, dev, body, now):
    entries = body.get("logs") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        entries = [body]
    for entry in entries[:50]:
        db.execute("INSERT INTO eink_logs (device_id, at, body) VALUES (?, ?, ?)",
                   (dev["id"], now, json.dumps(entry)[:4000]))
    db.execute("DELETE FROM eink_logs WHERE device_id = ? AND id NOT IN"
               " (SELECT id FROM eink_logs WHERE device_id = ? ORDER BY id DESC LIMIT ?)", (dev["id"], dev["id"], LOG_ROWS))
    db.commit()


def recent_logs(db, device_id, limit=10):
    logs = []
    for row in db.execute("SELECT at, body FROM eink_logs WHERE device_id = ? ORDER BY id DESC LIMIT ?", (device_id, limit)):
        try:
            entry = json.loads(row["body"])
        except ValueError:
            entry = row["body"]
        text = entry.get("message") or entry.get("log_message") or json.dumps(entry) if isinstance(entry, dict) else str(entry)
        logs.append({"at": row["at"], "text": str(text)[:300]})
    return logs


def battery_history(db, display_id, days, now):
    """A display's battery readings over the last `days`, for the chart, with a battery life estimate.

    Readings are stored as volts and turned into a percentage here, so a better voltage curve later
    redraws the whole history. Long ranges are averaged down to at most CHART_POINTS points.
    """
    dev = device_for_display(db, display_id)
    if dev is None:
        raise ServiceError("Not an e-ink display", 404)
    try:
        days = max(1.0, min(float(READINGS_KEPT_DAYS), float(days or 7)))
    except (TypeError, ValueError):
        raise ServiceError("days must be a number")
    start = now - days * 86400
    rows = db.execute("SELECT at, battery_v FROM eink_readings WHERE device_id = ? AND at >= ? ORDER BY at",
                      (dev["id"], start)).fetchall()
    bucket = max(1.0, days * 86400 / CHART_POINTS)
    groups = {}
    for row in rows:
        groups.setdefault(int((row["at"] - start) // bucket), []).append(row)
    samples = []
    for index in sorted(groups):
        group = groups[index]
        volts = sum(r["battery_v"] for r in group) / len(group)
        if battery_percent(volts) is None:
            continue
        samples.append({"at": round(sum(r["at"] for r in group) / len(group)), "volts": round(volts, 3),
                        "percent": battery_percent(volts)})
    # The estimate looks at the whole current discharge, however short a range the chart shows.
    every = db.execute("SELECT at, battery_v FROM eink_readings WHERE device_id = ? ORDER BY at", (dev["id"],)).fetchall()
    first = db.execute("SELECT MIN(at) FROM eink_readings WHERE device_id = ?", (dev["id"],)).fetchone()[0]
    return {"days": days, "from": round(start), "to": round(now), "mains": bool(dev["mains"]), "count": len(rows),
            "first_reading": round(first) if first else None, "samples": samples,
            "estimate": battery_estimate([(r["at"], battery_percent(r["battery_v"])) for r in every], now)}


def battery_estimate(points, now):
    """How fast the battery is falling and when it would be empty, from (time, percent) readings, or None.

    Only the current discharge counts: everything since the battery last rose by CHARGE_JUMP points
    (it was charged). A straight line is fitted to it. There is no estimate until a day of it is on
    record and it has fallen far enough to tell a trend from the noise in the readings.
    """
    points = [(at, pct) for at, pct in points if pct is not None]
    run_start = 0
    for i in range(1, len(points)):
        if points[i][1] - points[i - 1][1] >= CHARGE_JUMP:
            run_start = i
    run = points[run_start:]
    if len(run) < 3:
        return None
    hours = (run[-1][0] - run[0][0]) / 3600
    if hours < ESTIMATE_MIN_HOURS:
        return None
    n = len(run)
    mean_t = sum(at for at, _ in run) / n
    mean_p = sum(pct for _, pct in run) / n
    spread = sum((at - mean_t) ** 2 for at, _ in run)
    if not spread:
        return None
    slope = sum((at - mean_t) * (pct - mean_p) for at, pct in run) / spread      # points per second; negative when falling
    per_day = -slope * 86400
    fitted_now = mean_p + slope * (now - mean_t)
    fallen = -slope * (run[-1][0] - run[0][0])
    if fallen < ESTIMATE_MIN_DROP or per_day <= 0:
        return {"since": round(run[0][0]), "hours": round(hours, 1), "per_day": round(max(per_day, 0), 2), "days_left": None,
                "empty_at": None, "full_charge_days": None}
    days_left = max(0.0, fitted_now) / per_day
    return {"since": round(run[0][0]), "hours": round(hours, 1), "per_day": round(per_day, 2),
            "days_left": round(days_left, 1), "empty_at": round(now + days_left * 86400),
            "full_charge_days": round(100 / per_day, 1)}


def is_online(dev, now):
    """It made the wake it was last told to make (with some grace)."""
    if not dev["next_expected_at"]:
        return False
    return now <= dev["next_expected_at"] + max(120, dev["refresh_s"])


# --- schedules on the server's clock -----------------------------------------------
#
# The Pi agent applies schedules itself, on its own clock. For e-ink the server decides, in the
# server's local time. A period is {"on": "07:30", "off": "09:00", "days": [0, 1, ...]} where the
# days (Monday is 0) are the days it starts on; an off time earlier than on runs past midnight.

def _minutes(text):
    return int(text[:2]) * 60 + int(text[3:])


def period_active(period, when):
    on, off, minute = _minutes(period["on"]), _minutes(period["off"]), when.hour * 60 + when.minute
    today, yesterday = when.weekday(), (when.weekday() - 1) % 7
    if on < off:
        return today in period["days"] and on <= minute < off
    return (today in period["days"] and minute >= on) or (yesterday in period["days"] and minute < off)


def next_boundary(periods, when):
    """Seconds until any of the periods next starts or ends, or None when there are none."""
    best = None
    midnight = when.replace(hour=0, minute=0, second=0, microsecond=0)
    for period in periods:
        on, off = _minutes(period["on"]), _minutes(period["off"])
        for offset in range(-1, 8):
            day = midnight + timedelta(days=offset)
            if day.weekday() not in period["days"]:
                continue
            for edge in (day + timedelta(minutes=on), day + timedelta(minutes=off, days=0 if on < off else 1)):
                if edge > when and (best is None or edge < best):
                    best = edge
    return (best - when).total_seconds() if best else None


# --- what a display should show ------------------------------------------------------

def default_dither(row):
    """Photos and other people's sites are dithered; pages drawn in flat colour are not."""
    return "dither" if row["kind"] == "url" or row["module"] in ("image", "photos") else "none"


def _item(row, source, until=None):
    return {
        "key": str(row["id"]), "content_id": row["id"], "name": row["name"], "url": row["url"],
        "zoom": row["zoom"] or 1.0, "css": row["css"] or "", "kind": row["kind"], "module": row["module"],
        "dither": row["eink_dither"] if row["eink_dither"] in ("none", "dither") else default_dither(row),
        "refresh": row["eink_refresh"] or row["refresh_interval"] or 0,
        "source": source, "until": until,
    }


def _url_item(url, until):
    return {"key": "url:" + hashlib.sha1(url.encode()).hexdigest()[:12], "content_id": None, "name": url, "url": url,
            "zoom": 1.0, "css": "", "kind": "url", "module": "", "dither": "dither", "refresh": 0,
            "source": "override", "until": until}


def playlist_items(db, playlist_id):
    return db.execute(
        "SELECT c.*, pi.duration FROM playlist_items pi JOIN content_items c ON c.id = pi.content_id"
        " WHERE pi.playlist_id = ? ORDER BY pi.position, pi.id", (playlist_id,)).fetchall()


def current(db, display_id, now, min_interval=0):
    """What the display should show at `now`, with "next" (the item after it in a rotation), or None.

    A playlist rotates on the server's clock. An item's turn is never shorter than the device's
    shortest sleep, since the device cannot change the picture more often than it wakes.
    """
    override = db.execute("SELECT * FROM overrides WHERE display_id = ? AND expires_at > ? ORDER BY id DESC LIMIT 1",
                          (display_id, now)).fetchone()
    if override:
        row = None
        if override["content_id"]:
            row = db.execute("SELECT * FROM content_items WHERE id = ?", (override["content_id"],)).fetchone()
        return _item(row, "override", override["expires_at"]) if row else _url_item(override["url"], override["expires_at"])

    when = datetime.fromtimestamp(now)
    playlist_id, source = None, "playlist"
    for rule in services.playlist_rules(db, display_id):
        if period_active(rule, when):
            playlist_id, source = rule["playlist_id"], "rule"
            break
    if playlist_id is None:
        assigned = db.execute("SELECT playlist_id FROM assignments WHERE display_id = ?", (display_id,)).fetchone()
        playlist_id = assigned["playlist_id"] if assigned else None
    rows = playlist_items(db, playlist_id) if playlist_id else []
    if not rows:
        return None
    if len(rows) == 1:
        return _item(rows[0], source)
    turns = [max(int(r["duration"] or 0), int(min_interval), 1) for r in rows]
    position = now % sum(turns)
    for index, turn in enumerate(turns):
        if position < turn:
            break
        position -= turn
    item = _item(rows[index], source, now + turns[index] - position)
    item["next"] = _item(rows[(index + 1) % len(rows)], source)
    return item


def awake(db, display_id, now):
    """Inside the display's wake hours (its screen power schedules). No schedules means always."""
    schedules = services.display_schedules(db, display_id)
    return not schedules or any(period_active(s, datetime.fromtimestamp(now)) for s in schedules)


def _boundaries(db, display_id, now):
    periods = services.display_schedules(db, display_id) + services.playlist_rules(db, display_id)
    return next_boundary(periods, datetime.fromtimestamp(now))


def render_row(db, display_id, key):
    return db.execute("SELECT * FROM eink_renders WHERE display_id = ? AND key = ?", (display_id, key)).fetchone()


def wake_interval(db, dev, display_id, item, row, now):
    """Seconds the device should sleep: until the next thing that could change its picture."""
    refresh = item["refresh"] or dev["max_interval_s"]
    changes = [row["checked_at"] + refresh + RENDER_MARGIN, row["published_at"] + dev["heartbeat_s"] + RENDER_MARGIN]
    if item["until"]:
        changes.append(item["until"])
    boundary = _boundaries(db, display_id, now)
    if boundary is not None:
        changes.append(now + boundary)
    low = min(dev["min_interval_s"], 60) if dev["mains"] else dev["min_interval_s"]
    return int(max(low, min(dev["max_interval_s"], min(changes) - now)))


def display_reply(db, dev, base_url, data_dir, now):
    """Answer GET /api/display for a device whose key checked out. Never renders: it hands out what is ready."""
    if dev["agent_id"] is None:
        name = builtin(image_dir(data_dir, f"pending-{dev['id']}"), dev,
                       ["Adopt this display", "in Vital Signs", f"ID {dev['friendly_id']}"])
        return _reply(base_url, f"pending-{dev['id']}", name, ADOPT_RATE)

    display = db.execute("SELECT * FROM displays WHERE agent_id = ? AND kind = 'eink'", (dev["agent_id"],)).fetchone()
    folder = image_dir(data_dir, display["id"])
    low = min(dev["min_interval_s"], 60) if dev["mains"] else dev["min_interval_s"]
    item = current(db, display["id"], now, low)
    key = item["key"] if item else ""
    kept = dev["drawn_filename"] if image_path(data_dir, display["id"], dev["drawn_filename"]) else ""

    if not awake(db, display["id"], now) and not (item and item["source"] == "override"):
        # Outside wake hours the panel keeps its picture and the device sleeps until they start.
        boundary = _boundaries(db, display["id"], now) or OFF_HOURS_CAP
        filename = kept or builtin(folder, dev, ["Asleep", "outside wake hours"])
        rate, key = int(max(low, min(OFF_HOURS_CAP, boundary + 5))), dev["drawn_key"] if kept else ""
    elif item is None:
        filename, rate = builtin(folder, dev, ["No playlist assigned", "Choose one in Vital Signs"]), low
    else:
        row = render_row(db, display["id"], key)
        if row and image_path(data_dir, display["id"], row["filename"]):
            filename, rate = row["filename"], wake_interval(db, dev, display["id"], item, row, now)
        elif row and row["error"]:
            filename = kept or builtin(folder, dev, ["Could not draw", item["name"][:40], row["error"][:60]])
            rate, key = low, dev["drawn_key"] if kept else ""
        else:
            # Being drawn now. Come back for it soon, but only if something is actually drawing.
            filename = kept or builtin(folder, dev, ["Vital Signs", "Drawing the first picture…"])
            rate, key = (PENDING_RATE if renderer_alive(now) else low), dev["drawn_key"] if kept else ""

    changed = filename != dev["drawn_filename"]
    db.execute("UPDATE eink_devices SET refresh_s = ?, next_expected_at = ?, drawn_filename = ?, drawn_key = ?,"
               " drawn_at = ? WHERE id = ?",
               (rate, now + rate + 30, filename, key, now if changed else dev["drawn_at"], dev["id"]))
    return _reply(base_url, display["id"], filename, rate)


def _reply(base_url, folder, filename, rate):
    return {
        "status": 0,
        "image_url": f"{base_url}/api/eink/image/{folder}/{filename}.png",
        "filename": filename,
        "refresh_rate": int(rate),
        "update_firmware": False, "firmware_url": "", "reset_firmware": False,
        "special_function": "sleep",
    }


# --- fields for the admin UI, the control API and Home Assistant ---------------------

def display_fields(db, row, now):
    """What services._display_dict adds for an e-ink display."""
    dev = db.execute("SELECT * FROM eink_devices WHERE agent_id = ?", (row["agent_id"],)).fetchone()
    if dev is None:
        return {}
    telemetry = json.loads(dev["telemetry"] or "{}")
    drawn = render_row(db, row["id"], dev["drawn_key"]) if dev["drawn_key"] else None
    low = min(dev["min_interval_s"], 60) if dev["mains"] else dev["min_interval_s"]
    item = current(db, row["id"], now, low)
    wanted = render_row(db, row["id"], item["key"]) if item else None
    errors = []
    if wanted and wanted["error"]:
        errors.append({"name": item["name"], "url": item["url"], "error": wanted["error"]})
    showing = None
    if drawn:
        showing = {"name": drawn["name"], "override": bool(item and item["source"] == "override" and item["key"] == dev["drawn_key"])}
    percent = None if dev["mains"] else battery_percent(telemetry.get("battery_v"))
    return {
        "online": is_online(dev, now),
        "version_drift": False,
        "screen_on": True,
        "current": showing,
        "errors": errors,
        "screenshot_url": f"/eink/{row['id']}.png?t={dev['drawn_filename']}" if dev["drawn_filename"] else None,
        "eink": {
            "friendly_id": dev["friendly_id"], "model": dev["model"], "model_name": model_name(dev["model"]),
            "palette": palette_name(dev["model"]), "firmware": dev["fw_version"],
            "battery_v": telemetry.get("battery_v"), "battery_percent": percent, "rssi": telemetry.get("rssi"),
            "wifi": wifi_word(telemetry.get("rssi")), "usb": telemetry.get("usb"),
            "wake_reason": telemetry.get("wake_reason") or "",
            "last_seen": dev["last_seen"], "next_expected_at": dev["next_expected_at"], "refresh_s": dev["refresh_s"],
            "mains": bool(dev["mains"]), "min_interval_s": dev["min_interval_s"], "max_interval_s": dev["max_interval_s"],
            "heartbeat_s": dev["heartbeat_s"], "footer": bool(dev["footer"]),
            "insets": dict(zip(SIDES, insets(dev))), "min_text_px": dev["min_text_px"],
            "drawn_at": dev["drawn_at"], "checked_at": drawn["published_at"] if drawn else 0,
            "stale": bool(drawn and drawn["stale"]),
            "awake": awake(db, row["id"], now),
            "pairing": dev["pair_until"] > now,
            "renderer": renderer_status(),
        },
    }


def device_facts(db, agent_id):
    """A device's readings as (label, value, is a warning) rows, for its card on the dashboard."""
    dev = db.execute("SELECT * FROM eink_devices WHERE agent_id = ?", (agent_id,)).fetchone()
    if dev is None:
        return []
    telemetry = json.loads(dev["telemetry"] or "{}")
    facts = [("Model", model_name(dev["model"]), False)]
    percent = battery_percent(telemetry.get("battery_v"))
    if dev["mains"]:
        facts.append(("Power", "mains", False))
    elif percent is not None:
        facts.append(("Battery", f"{percent}% ({telemetry['battery_v']:.2f} V)", percent < 20))
    if isinstance(telemetry.get("rssi"), int) and telemetry["rssi"] < 0:
        facts.append(("Wi-Fi", f"{telemetry['rssi']} dBm ({wifi_word(telemetry['rssi'])})", telemetry["rssi"] < -78))
    if dev["last_seen"]:
        facts.append(("Checked in", clock(dev["last_seen"]), False))
    if dev["next_expected_at"]:
        facts.append(("Next due", clock(dev["next_expected_at"]), False))
    return [{"label": label, "value": value, "warn": warn} for label, value, warn in facts]


# --- pictures -----------------------------------------------------------------------

def _palette_image(name):
    inks = PALETTES[name]
    flat = [channel for ink in inks for channel in ink]
    image = Image.new("P", (1, 1))
    image.putpalette(flat + flat[:3] * (256 - len(inks)))     # pad with the first ink so nothing else can match
    return image


def quantise(image, palette, dither="none"):
    """Map an image to the palette's inks. `none` takes each pixel to its nearest ink, which keeps flat
    colour and text crisp; `dither` mixes inks to make the tones between them, which is right for photos."""
    image = image.convert("RGB")
    if dither == "dither":
        used = image.getcolors(maxcolors=len(PALETTES[palette]))
        if used is None or any(colour not in PALETTES[palette] for _, colour in used):
            return _dither_photo(image, palette)        # anything already in pure inks needs no mixing
    # Text is drawn with soft grey edges. Cutting those at mid-grey breaks thin strokes apart, so they are
    # darkened first: more of each edge lands on the ink side. Pure inks are not moved by this.
    image = image.point([round(255 * (i / 255) ** TEXT_INK_GAMMA) for i in range(256)] * 3)
    return image.quantize(palette=_palette_image(palette), dither=Image.Dither.NONE)


def _dither_photo(image, palette):
    """Dither a photo, or a page not made for e-ink, into the palette's inks.

    Plain Floyd-Steinberg against pure red and yellow peppers skies with black dots and solid colours
    with specks, because it tries to make up every difference between the photo and the inks. So:
    the picture is first stretched to use the whole tonal range and lightened, its strong colours are
    pushed and its faint ones (a warm grey, an off-white) are taken to grey;
    inks are chosen by how they look (INK_LOOKS), comparing brightness and colour separately so a
    grey never turns red; and the error is spread Atkinson's way, which lets a quarter of it go.
    About a second for an 800 x 480 panel.
    """
    image = ImageOps.autocontrast(image, cutoff=PHOTO_CONTRAST_CUTOFF, preserve_tone=True)
    hue, saturation, value = image.convert("HSV").split()
    span = PHOTO_COLOUR_FROM - PHOTO_GREY_BELOW
    curve = [0 if s <= PHOTO_GREY_BELOW else min(255, round(s * PHOTO_SATURATION * min(1.0, (s - PHOTO_GREY_BELOW) / span)))
             for s in range(256)]
    image = Image.merge("HSV", (hue, saturation.point(curve), value)).convert("RGB")
    image = image.point([round(255 * (i / 255) ** PHOTO_GAMMA) for i in range(256)] * 3)

    width, height = image.size
    data = image.tobytes()
    pad = [0.0] * 12        # room for the error pushed past the right-hand edge
    rows = [[float(v) for v in data[y * width * 3:(y + 1) * width * 3]] + pad for y in range(height)]
    rows += [[0.0] * (width * 3 + 12), [0.0] * (width * 3 + 12)]
    inks = []
    for r, g, b in INK_LOOKS[palette]:
        luma = 0.299 * r + 0.587 * g + 0.114 * b
        inks.append((r, g, b, luma, b - luma, r - luma))
    out = bytearray(width * height)
    share, weight = PHOTO_ERROR_KEPT / 6, PHOTO_CHROMA_WEIGHT
    for y in range(height):
        row, below, below2 = rows[y], rows[y + 1], rows[y + 2]
        start = y * width
        for x in range(width):
            i = x * 3
            r, g, b = row[i], row[i + 1], row[i + 2]
            r = 0.0 if r < 0 else 255.0 if r > 255 else r
            g = 0.0 if g < 0 else 255.0 if g > 255 else g
            b = 0.0 if b < 0 else 255.0 if b > 255 else b
            luma = 0.299 * r + 0.587 * g + 0.114 * b
            blue, red = b - luma, r - luma
            best, nearest = 0, 1e18
            for n, ink in enumerate(inks):
                distance = (luma - ink[3]) ** 2 + weight * ((blue - ink[4]) ** 2 + (red - ink[5]) ** 2)
                if distance < nearest:
                    nearest, best = distance, n
            out[start + x] = best
            ink = inks[best]
            er, eg, eb = (r - ink[0]) * share, (g - ink[1]) * share, (b - ink[2]) * share
            row[i + 3] += er; row[i + 4] += eg; row[i + 5] += eb                # noqa: E702  (right)
            row[i + 6] += er; row[i + 7] += eg; row[i + 8] += eb                # noqa: E702  (two right)
            if x:
                below[i - 3] += er; below[i - 2] += eg; below[i - 1] += eb      # noqa: E702  (below left)
            below[i] += er; below[i + 1] += eg; below[i + 2] += eb              # noqa: E702  (below)
            below[i + 3] += er; below[i + 4] += eg; below[i + 5] += eb          # noqa: E702  (below right)
            below2[i] += er; below2[i + 1] += eg; below2[i + 2] += eb           # noqa: E702  (two below)
    result = Image.frombytes("P", (width, height), bytes(out))
    result.putpalette([channel for ink in PALETTES[palette] for channel in ink])
    return result


def png_bytes(image, palette):
    """An indexed PNG at the smallest bit depth that holds the palette (2 bits for four inks)."""
    inks = PALETTES[palette]
    image = image.copy()
    image.putpalette([channel for ink in inks for channel in ink])
    out = io.BytesIO()
    image.save(out, "PNG", bits=max(1, (len(inks) - 1).bit_length()), optimize=True)
    return out.getvalue()


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:       # Pillow before 10.1 has only a small bitmap font
        return ImageFont.load_default()


def _draw(image):
    draw = ImageDraw.Draw(image)
    draw.fontmode = "1"     # no anti-aliasing: grey edges would turn to speckle on the panel
    return draw


def builtin(folder, dev, lines):
    """A plain message picture for the device (waiting, not adopted, nothing assigned). Returns its file name."""
    palette = palette_name(dev["model"])
    width, height = (dev["width"], dev["height"]) if dev["width"] else DEFAULT_SIZE
    image = Image.new("RGB", (width, height), WHITE)
    draw = _draw(image)
    top, right, bottom, left = insets(dev)
    seen_w, seen_h = width - left - right, height - top - bottom     # the part not behind the frame
    sizes = [round(seen_h * 0.11)] + [round(seen_h * 0.065)] * (len(lines) - 1)
    total = sum(sizes) + round(seen_h * 0.04) * (len(lines) - 1)
    y = top + (seen_h - total) / 2
    for text, size in zip(lines, sizes):
        font = _font(size)
        while size > 10 and draw.textlength(text, font=font) > seen_w * 0.92:
            size -= 2
            font = _font(size)
        draw.text((left + (seen_w - draw.textlength(text, font=font)) / 2, y), text, font=font, fill=BLACK)
        y += size + round(seen_h * 0.04)
    data = png_bytes(quantise(image, palette), palette)
    name = hashlib.sha256(data).hexdigest()[:16]
    path = folder / f"{name}.png"
    if not path.is_file():
        path.write_bytes(data)
    return name


def footer_image(width, palette, info):
    """The strip under every picture: when it was checked, what it is, and the device's battery and signal."""
    image = Image.new("RGB", (width, FOOTER_PX), WHITE)
    draw = _draw(image)
    draw.line([(0, 0), (width, 0)], fill=BLACK, width=2)
    font, y = _font(17), 6
    left = info["left"]
    draw.text((8, y), left, font=font, fill=RED if info.get("stale") and palette == "bwry" else BLACK)
    right = info.get("right") or ""
    right_width = draw.textlength(right, font=font)
    draw.text((width - 8 - right_width, y), right, font=font, fill=RED if info.get("low") and palette == "bwry" else BLACK)
    # The name takes what is left in the middle, shortened to fit.
    start = 8 + draw.textlength(left, font=font) + 16
    room = width - 8 - right_width - 16 - start
    name = info.get("name") or ""
    while name and draw.textlength(name, font=font) > room:
        name = name[:-2].rstrip() + "…" if len(name) > 2 else ""
    if name:
        draw.text((start + (room - draw.textlength(name, font=font)) / 2, y), name, font=font, fill=BLACK)
    return image


def footer_info(dev, item, data_at, stale, sources, now):
    telemetry = json.loads(dev["telemetry"] or "{}")
    if stale:
        left = f"Data from {clock(data_at)}" if data_at else "No data yet"
    else:
        left = f"Checked {clock(now)}"
    for source in sources[:2]:
        left += f"  {source['label']} {clock(source['at'])}"
    parts, low = [], False
    if not dev["mains"]:
        percent = battery_percent(telemetry.get("battery_v"))
        if percent is not None:
            parts.append(f"Battery {percent}%")
            low = percent < 20
        word = wifi_word(telemetry.get("rssi"))
        if word:
            parts.append(f"Wi-Fi {word}")
    name = item["name"] if item["content_id"] else urlsplit(item["url"]).netloc
    return {"left": left, "name": name, "right": "  ".join(parts), "stale": stale, "low": low}


def page_url(item, display, palette, base_url, min_text=0):
    """The address the renderer loads: what a Pi would load, plus the e-ink hints for local pages."""
    url = item["url"]
    if not services.is_local_url(url):
        return url
    parts = urlsplit(base_url + url)
    query = parse_qsl(parts.query, keep_blank_values=True) + [
        ("display", display["name"]), ("width", display["width"]), ("height", display["height"]),
        ("orientation", display["orientation"]), ("medium", "eink"), ("palette", palette)]
    if min_text:
        query.append(("mintext", min_text))
    return urlunsplit(parts._replace(query=urlencode(query)))


def freshness(data_dir, item, refresh, now):
    """(when the item's data was last fetched successfully, whether that is too long ago).

    Modules fetch through the server, which keeps the last good reply on disk, so that file's time is
    the last success. Anything else has no data source we can see and counts as fresh.
    """
    from . import modules       # modules imports services too; keep this import out of module load order
    if item["kind"] != "module":
        return now, False
    manifest = modules.list_modules().get(item["module"]) or {}
    provider = modules.PROVIDERS.get(manifest.get("data"))
    if provider is None:
        return now, False
    data_at = modules.last_success(data_dir, item["content_id"])
    limit = 2 * max(provider[1], refresh) + 120
    return data_at, not data_at or now - data_at > limit


def item_spec(item, display, dev):
    """Changes whenever the picture would: the page's settings, the panel, and the footer switch."""
    return hashlib.sha1(json.dumps([item["url"], item["zoom"], item["css"], item["dither"], display["width"],
                                    display["height"], palette_name(dev["model"]), dev["footer"],
                                    *(insets(dev) if any(insets(dev)) else ()),
                                    *((f"text{dev['min_text_px']}",) if dev["min_text_px"] else ())]).encode()).hexdigest()[:16]


class RenderError(Exception):
    pass


# Run in every frame before the picture is taken. Pages size their text for a big screen; on a small panel
# some of it comes out too small to survive being cut to pure ink. Any element that holds text of its own
# and is smaller than the display's "smallest text" is enlarged to it. Text inside an SVG is measured by
# how big it comes out on screen, since its own units are scaled by the drawing.
ENLARGE_SMALL_TEXT = """(min) => {
  const walker = document.createTreeWalker(document.documentElement, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  while (walker.nextNode()) {
    const el = walker.currentNode.parentElement;
    if (!el || seen.has(el) || !walker.currentNode.nodeValue.trim()) continue;
    seen.add(el);
    if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'TITLE'].includes(el.tagName.toUpperCase())) continue;
    const size = parseFloat(getComputedStyle(el).fontSize);
    if (!size) continue;
    let scale = 1;
    if (el instanceof SVGElement && el.getScreenCTM) {
      const m = el.getScreenCTM();
      if (m) scale = Math.sqrt(Math.abs(m.a * m.d - m.b * m.c)) || 1;
    }
    for (let up = el; up && up.style; up = up.parentElement) {
      const zoom = parseFloat(getComputedStyle(up).zoom);
      if (zoom && !(el instanceof SVGElement)) scale *= zoom;
    }
    if (size * scale < min - 0.25) el.style.setProperty('font-size', (min / scale) + 'px', 'important');
  }
}"""


class Browser:
    """A headless Chromium, through Playwright. Only the renderer thread may use it."""

    def __init__(self, executable=None):
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(
                executable_path=executable or None, args=["--disable-lcd-text", "--hide-scrollbars"])
        except Exception:
            self._playwright.stop()
            raise

    def shoot(self, url, width, height, zoom=1.0, css="", min_text=0):
        """Load a page and return (PNG bytes, the source times it declared with VS.sourceTime).
        `min_text` is the smallest text allowed, in pixels: anything smaller is enlarged to it."""
        context = self._browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1,
                                            reduced_motion="reduce", color_scheme="light")
        try:
            page = context.new_page()
            try:
                response = page.goto(url, wait_until="load", timeout=READY_TIMEOUT * 1000)
            except Exception as e:
                raise RenderError(_short_error(e))
            if response is not None and response.status >= 400:
                raise RenderError(f"HTTP {response.status}")
            if abs(zoom - 1.0) > 0.001:
                css = f"html {{ zoom: {zoom}; }}\n{css}"
            if css.strip():
                page.add_style_tag(content=css)
            if page.evaluate("typeof window.VS === 'object'"):
                try:        # our own pages say when they are fully drawn
                    page.wait_for_function("window.__vsReady === true", timeout=READY_TIMEOUT * 1000)
                except Exception:
                    raise RenderError("the page did not finish loading")
                page.wait_for_timeout(150)
            else:
                page.wait_for_timeout(2000)
            sources = page.evaluate("(window.__vsSources || []).slice(0, 4)")
            if min_text:
                for frame in page.frames:       # a split screen's panes are frames, and so are a site's own
                    try:
                        frame.evaluate(ENLARGE_SMALL_TEXT, min_text)
                    except Exception:           # a frame that went away or refuses scripts keeps its text as it is
                        pass
                page.wait_for_timeout(100)
            return page.screenshot(type="png"), sources
        finally:
            context.close()

    def close(self):
        try:
            self._browser.close()
        finally:
            self._playwright.stop()


def _short_error(error):
    text = str(error).splitlines()[0] if str(error) else type(error).__name__
    match = re.search(r"net::(ERR_\w+)", text)
    if match:
        return {"ERR_NAME_NOT_RESOLVED": "its address could not be looked up", "ERR_CONNECTION_REFUSED": "connection refused",
                "ERR_CONNECTION_TIMED_OUT": "it did not answer in time"}.get(match.group(1), match.group(1))
    return "it did not load in time" if "Timeout" in text else text[:120]


def render_item(db, data_dir, base_url, display, dev, item, browser, now, force_publish=False):
    """Draw one item for one display and store it. Returns True if a new picture was published."""
    palette = palette_name(dev["model"])
    width, height = display["width"] or DEFAULT_SIZE[0], display["height"] or DEFAULT_SIZE[1]
    # The page and the footer are drawn in the part of the panel the frame leaves visible; the rest stays white.
    top, right, bottom, left = insets(dev)
    seen_width = width - left - right
    body_height = height - top - bottom - (FOOTER_PX if dev["footer"] else 0)
    spec = item_spec(item, display, dev)
    row = render_row(db, display["id"], item["key"])
    try:
        shot, sources = browser.shoot(page_url(item, display, palette, base_url, dev["min_text_px"]), seen_width, body_height,
                                      item["zoom"], item["css"], dev["min_text_px"])
        body = Image.open(io.BytesIO(shot)).convert("RGB")
    except RenderError as e:
        log.warning("E-ink: could not draw %s for display %s: %s", item["name"], display["id"], e)
        db.execute(
            "INSERT INTO eink_renders (display_id, key, name, spec, error, failed_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(display_id, key) DO UPDATE SET name = excluded.name, error = excluded.error,"
            " failed_at = excluded.failed_at, stale = 1",
            (display["id"], item["key"], item["name"], spec, str(e), now))
        db.commit()
        return False
    if body.size != (seen_width, body_height):
        body = body.resize((seen_width, body_height))
    body = quantise(body, palette, item["dither"])
    data_at, stale = freshness(data_dir, item, item["refresh"] or dev["max_interval_s"], now)
    sources = [{"label": str(s.get("label", ""))[:16], "at": float(s["at"])} for s in sources or []
               if isinstance(s, dict) and isinstance(s.get("at"), (int, float))]
    body_hash = hashlib.sha256(body.tobytes() + (b"stale" if stale else b"")).hexdigest()[:32]

    unchanged = (row is not None and row["spec"] == spec and row["body_hash"] == body_hash
                 and image_path(data_dir, display["id"], row["filename"]))
    if unchanged and not force_publish and now - row["published_at"] < dev["heartbeat_s"]:
        # Same picture: leave the panel alone (a new footer time is not worth a 20 second refresh).
        db.execute("UPDATE eink_renders SET checked_at = ?, data_at = ?, stale = ?, error = '', failed_at = 0, name = ?"
                   " WHERE display_id = ? AND key = ?", (now, data_at, int(stale), item["name"], display["id"], item["key"]))
        db.commit()
        return False

    picture = Image.new("RGB", (width, height), WHITE)
    picture.paste(body.convert("RGB"), (left, top))
    if dev["footer"]:
        picture.paste(footer_image(seen_width, palette, footer_info(dev, item, data_at, stale, sources, now)),
                      (left, top + body_height))
    data = png_bytes(quantise(picture, palette), palette)
    filename = hashlib.sha256(data).hexdigest()[:16]
    folder = image_dir(data_dir, display["id"])
    (folder / f"{filename}.png").write_bytes(data)
    db.execute(
        "INSERT INTO eink_renders (display_id, key, name, spec, filename, body_hash, published_at, checked_at, data_at, stale)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(display_id, key) DO UPDATE SET name = excluded.name, spec = excluded.spec,"
        " filename = excluded.filename, body_hash = excluded.body_hash, published_at = excluded.published_at,"
        " checked_at = excluded.checked_at, data_at = excluded.data_at, stale = excluded.stale, error = '', failed_at = 0",
        (display["id"], item["key"], item["name"], spec, filename, body_hash, now, now, data_at, int(stale)))
    db.commit()
    _prune(db, folder, display["id"], dev)
    return True


def _prune(db, folder, display_id, dev):
    """Keep the pictures still in use and the newest few others."""
    in_use = {r["filename"] for r in db.execute("SELECT filename FROM eink_renders WHERE display_id = ?", (display_id,))}
    in_use.add(dev["drawn_filename"])
    files = sorted(folder.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[KEEP_FILES:]:
        if path.stem not in in_use:
            path.unlink(missing_ok=True)


def due_jobs(db, now):
    """(display row, device row, item, republish) for every picture that needs drawing now."""
    jobs = []
    ready = db.execute("SELECT * FROM content_items WHERE eink_ready = 1 AND kind != 'rotation'").fetchall()
    for display in db.execute("SELECT * FROM displays WHERE kind = 'eink'").fetchall():
        dev = db.execute("SELECT * FROM eink_devices WHERE agent_id = ?", (display["agent_id"],)).fetchone()
        if dev is None:
            continue
        low = min(dev["min_interval_s"], 60) if dev["mains"] else dev["min_interval_s"]
        item = current(db, display["id"], now, low)
        wanted = {}
        if item:
            wanted[item["key"]] = item
            upcoming = item.get("next")
            if upcoming and item["until"] - now <= max(low, 120):      # its turn is close: have it ready
                wanted.setdefault(upcoming["key"], upcoming)
        for row in ready:
            wanted.setdefault(str(row["id"]), _item(row, "ready"))
        for key, target in wanted.items():
            row = render_row(db, display["id"], key)
            refresh = target["refresh"] or dev["max_interval_s"]
            shown = item is not None and key == item["key"]
            if row is None or row["spec"] != item_spec(target, display, dev):
                jobs.append((display, dev, target, False))
            elif row["failed_at"] and now - row["failed_at"] < RETRY_FAILED:
                continue
            elif now - row["checked_at"] >= refresh or row["failed_at"]:
                jobs.append((display, dev, target, False))
            elif shown and now - row["published_at"] >= dev["heartbeat_s"]:
                jobs.append((display, dev, target, True))
        # Forget pictures for things this display no longer shows.
        keys = [r["key"] for r in db.execute("SELECT key FROM eink_renders WHERE display_id = ?", (display["id"],))]
        stale_keys = [k for k in keys if k not in wanted and k != dev["drawn_key"]]
        if len(stale_keys) > 10:
            db.executemany("DELETE FROM eink_renders WHERE display_id = ? AND key = ?", [(display["id"], k) for k in stale_keys])
            db.commit()
    return jobs


def render_due(db, data_dir, base_url, browser, now=None):
    """Draw everything that is due. Returns how many pictures were published."""
    published = 0
    for display, dev, item, republish in due_jobs(db, now or time.time()):
        if render_item(db, data_dir, base_url, display, dev, item, browser, now or time.time(), republish):
            published += 1
    return published


# --- the renderer thread -----------------------------------------------------------

class Renderer(threading.Thread):
    """Draws due pictures every few seconds. Starts Chromium only once there is an e-ink display to draw for."""

    PASS_SECONDS = 5

    def __init__(self, app):
        super().__init__(name="eink-renderer", daemon=True)
        cfg = app.config["VS"]
        self.db_path = app.config["DB_PATH"]
        self.data_dir = cfg["data_dir"]
        self.base_url = f"http://127.0.0.1:{cfg['port']}"
        self.executable = cfg.get("eink_chromium") or None
        self.browser = None
        self.status = "Idle"
        self.alive_at = 0.0
        self.failed_at = 0.0
        self._wake = threading.Event()
        self._halt = threading.Event()

    def wake(self):
        self._wake.set()

    def stop(self):
        self._halt.set()
        self._wake.set()

    def _ensure_browser(self):
        if self.browser is not None:
            return True
        if time.time() - self.failed_at < 300:      # do not hammer a launch that just failed
            return False
        try:
            self.browser = Browser(self.executable)
            self.status = "Ready"
            return True
        except ImportError:
            self.status = "Renderer unavailable: Playwright is not installed. Re-run deploy/install-server.sh."
        except Exception as e:
            self.status = f"Renderer unavailable: Chromium did not start ({_short_error(e)}). Re-run deploy/install-server.sh."
        self.failed_at = time.time()
        log.warning("E-ink: %s", self.status)
        return False

    def run(self):
        while not self._halt.is_set():
            db = None
            try:
                # Inside the try: a locked database (a slow disk during the nightly Proxmox backup) must cost
                # one pass, not the thread.
                db = database.connect(self.db_path)
                jobs = due_jobs(db, time.time())
                if jobs and self._ensure_browser():
                    for display, dev, item, republish in jobs:
                        self.alive_at = time.time()
                        try:
                            render_item(db, self.data_dir, self.base_url, display, dev, item, self.browser, time.time(), republish)
                        except Exception as e:      # Chromium died or lost its connection: start a fresh one next pass
                            log.warning("E-ink: the browser failed (%s); restarting it", _short_error(e))
                            try:
                                self.browser.close()
                            except Exception:
                                pass
                            self.browser = None
                            break
                if self.browser is not None or not jobs:
                    self.alive_at = time.time()
            except Exception:
                log.exception("E-ink: render pass failed")
            finally:
                if db is not None:
                    db.close()
            self._wake.wait(self.PASS_SECONDS)
            self._wake.clear()
        if self.browser is not None:
            self.browser.close()


renderer = None     # the running Renderer, when the server was started normally


def start(app):
    global renderer
    renderer = Renderer(app)
    renderer.start()
    return renderer


def wake_renderer():
    if renderer is not None:
        renderer.wake()


def renderer_alive(now):
    return renderer is not None and now - renderer.alive_at < 60


def renderer_status():
    if renderer is None:
        return "Not running (the server was not started normally)"
    if not renderer.is_alive():
        return "Stopped unexpectedly: restart the server and check its log"
    return renderer.status
