"""Operations shared by the admin UI, the control API and the agent API."""
import hashlib
import json
import re
import time

from shared.protocol import COMMAND_TYPES, PROTOCOL_VERSION, git_sha

ONLINE_SECONDS = 15
COMMAND_TTL = 300

SERVER_SHA = git_sha()

_EXTERNAL_URL = re.compile(r"^https?://\S+$")
_LOCAL_URL = re.compile(r"^/pages/[A-Za-z0-9][A-Za-z0-9_.-]*/\S*$")
_CLOCK_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ServiceError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def is_local_url(url):
    return bool(_LOCAL_URL.match(url))


def check_url(url):
    url = (url or "").strip()
    if not (_EXTERNAL_URL.match(url) or _LOCAL_URL.match(url)):
        raise ServiceError("URL must start with http://, https:// or /pages/<name>/")
    return url


# --- displays ---------------------------------------------------------------

def _display_dict(db, row, now):
    status = json.loads(row["status"] or "{}")
    online = now - row["last_seen"] < ONLINE_SECONDS
    playlist = db.execute(
        "SELECT p.id, p.name FROM assignments a JOIN playlists p ON p.id = a.playlist_id"
        " WHERE a.display_id = ?", (row["id"],)).fetchone()
    override = db.execute(
        "SELECT * FROM overrides WHERE display_id = ? AND expires_at > ? ORDER BY id DESC LIMIT 1",
        (row["id"], now)).fetchone()
    return {
        "id": row["id"],
        "ref": f"{row['hostname']}:{row['name']}",
        "agent_id": row["agent_id"],
        "agent": row["hostname"],
        "connector": row["connector"],
        "name": row["name"],
        "online": online,
        "last_seen": row["last_seen"],
        "width": row["width"],
        "height": row["height"],
        "orientation": row["orientation"],
        "zoom": row["zoom"],
        "agent_sha": row["git_sha"],
        "server_sha": SERVER_SHA,
        "agent_protocol": row["protocol_version"],
        "version_drift": row["git_sha"] != SERVER_SHA or row["protocol_version"] != PROTOCOL_VERSION,
        "playlist": dict(playlist) if playlist else None,
        "override": {
            "url": override["url"],
            "expires_at": override["expires_at"],
            "remaining_s": round(override["expires_at"] - now),
        } if override else None,
        "power_schedule": json.loads(row["power_schedule"]) if row["power_schedule"] else None,
        "screen_on": status.get("screen_on") is not False,
        "browser": status.get("browser", ""),
        "placement_ok": status.get("placement_ok", True),
        "current": status.get("current"),
        "errors": status.get("errors", []),
        "screenshot_url": f"/screenshots/{row['id']}.jpg?t={int(row['screenshot_at'])}"
        if row["screenshot_at"] else None,
    }


_DISPLAY_SELECT = (
    "SELECT d.*, a.hostname, a.last_seen, a.git_sha, a.protocol_version"
    " FROM displays d JOIN agents a ON a.id = d.agent_id")


def list_displays(db):
    now = time.time()
    rows = db.execute(_DISPLAY_SELECT + " ORDER BY a.hostname, d.name, d.connector").fetchall()
    return [_display_dict(db, r, now) for r in rows]


def get_display(db, ref):
    """Look a display up by numeric id or "<hostname>:<name>"."""
    ref = str(ref)
    if ref.isdigit():
        row = db.execute(_DISPLAY_SELECT + " WHERE d.id = ?", (int(ref),)).fetchone()
    else:
        host, _, name = ref.rpartition(":")
        row = db.execute(_DISPLAY_SELECT + " WHERE a.hostname = ? AND d.name = ?",
                         (host, name)).fetchone()
    if row is None:
        raise ServiceError(f"No such display: {ref}", 404)
    return _display_dict(db, row, time.time())


def assign(db, display_id, playlist_id):
    if playlist_id is None:
        db.execute("DELETE FROM assignments WHERE display_id = ?", (display_id,))
    else:
        if not db.execute("SELECT 1 FROM playlists WHERE id = ?", (playlist_id,)).fetchone():
            raise ServiceError("No such playlist", 404)
        db.execute(
            "INSERT INTO assignments (display_id, playlist_id) VALUES (?, ?)"
            " ON CONFLICT(display_id) DO UPDATE SET playlist_id = excluded.playlist_id",
            (display_id, playlist_id))
    db.commit()


def push_override(db, display_id, url, minutes):
    url = check_url(url)
    try:
        minutes = float(minutes)
    except (TypeError, ValueError):
        raise ServiceError("minutes must be a number")
    if not 0 < minutes <= 24 * 60:
        raise ServiceError("minutes must be between 0 and 1440")
    db.execute("DELETE FROM overrides WHERE display_id = ?", (display_id,))
    db.execute("INSERT INTO overrides (display_id, url, expires_at) VALUES (?, ?, ?)",
               (display_id, url, time.time() + minutes * 60))
    db.commit()


def clear_override(db, display_id):
    db.execute("DELETE FROM overrides WHERE display_id = ?", (display_id,))
    db.commit()


def set_display_zoom(db, display_id, zoom):
    try:
        zoom = float(zoom)
    except (TypeError, ValueError):
        raise ServiceError("zoom must be a number")
    if not 0.1 <= zoom <= 10:
        raise ServiceError("zoom must be between 0.1 and 10")
    db.execute("UPDATE displays SET zoom = ? WHERE id = ?", (zoom, display_id))
    db.commit()


def set_power_schedule(db, display_id, schedule):
    """Set when a display's screen is powered. None keeps it on all the time.

    A schedule is {"on": "HH:MM", "off": "HH:MM", "days": [0-6]} with Monday as 0.
    The agent applies it in the Pi's local time, so it keeps working offline.
    """
    value = ""
    if schedule is not None:
        on, off = str(schedule.get("on") or ""), str(schedule.get("off") or "")
        if not (_CLOCK_TIME.match(on) and _CLOCK_TIME.match(off)):
            raise ServiceError("on and off must be times like 07:30")
        if on == off:
            raise ServiceError("on and off times must differ")
        days = sorted({int(d) for d in schedule.get("days") or [] if str(d) in list("0123456")})
        if not days:
            raise ServiceError("Choose at least one day")
        value = json.dumps({"on": on, "off": off, "days": days})
    db.execute("UPDATE displays SET power_schedule = ? WHERE id = ?", (value, display_id))
    db.commit()


def queue_command(db, display, type_, args=None):
    if type_ not in COMMAND_TYPES:
        raise ServiceError(f"Unknown command: {type_}")
    db.execute(
        "INSERT INTO commands (agent_id, connector, type, args, created_at) VALUES (?, ?, ?, ?, ?)",
        (display["agent_id"], display["connector"], type_, json.dumps(args or {}), time.time()))
    db.commit()


def list_playlists(db):
    rows = db.execute(
        "SELECT p.id, p.name, COUNT(pi.id) AS item_count FROM playlists p"
        " LEFT JOIN playlist_items pi ON pi.playlist_id = p.id GROUP BY p.id ORDER BY p.name").fetchall()
    return [dict(r) for r in rows]


# --- agent side -------------------------------------------------------------

def desired_state(db, agent_id):
    """Desired state for every display of an agent, plus its revision.

    The revision is a hash of the state, so anything that changes what a
    display should show (including an override expiring) changes it.
    """
    now = time.time()
    db.execute("DELETE FROM overrides WHERE expires_at <= ?", (now,))
    state = {}
    remaining = {}
    for d in db.execute("SELECT * FROM displays WHERE agent_id = ?", (agent_id,)).fetchall():
        items = db.execute(
            "SELECT pi.id, pi.duration, c.id AS content_id, c.name, c.url, c.zoom,"
            " c.refresh_interval, c.css"
            " FROM assignments a"
            " JOIN playlist_items pi ON pi.playlist_id = a.playlist_id"
            " JOIN content_items c ON c.id = pi.content_id"
            " WHERE a.display_id = ? ORDER BY pi.position, pi.id", (d["id"],)).fetchall()
        ov = db.execute("SELECT * FROM overrides WHERE display_id = ? ORDER BY id DESC LIMIT 1",
                        (d["id"],)).fetchone()
        state[d["connector"]] = {
            "display_zoom": d["zoom"],
            "power": json.loads(d["power_schedule"]) if d["power_schedule"] else None,
            "items": [{
                "id": i["id"],
                "content_id": i["content_id"],
                "name": i["name"],
                "url": i["url"],
                "local": is_local_url(i["url"]),
                "duration": i["duration"],
                "zoom": i["zoom"],
                "refresh": i["refresh_interval"],
                "css": i["css"],
            } for i in items],
            "override": {"id": ov["id"], "url": ov["url"], "local": is_local_url(ov["url"])}
            if ov else None,
        }
        if ov:
            remaining[d["connector"]] = round(ov["expires_at"] - now, 1)
    revision = hashlib.sha1(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16]
    for connector, seconds in remaining.items():
        state[connector]["override"]["remaining_s"] = seconds
    return state, revision


def record_poll(db, body):
    """Register or update the agent and its displays. Returns the agent id."""
    hostname = body.get("hostname")
    if not isinstance(hostname, str) or not 0 < len(hostname) <= 255:
        raise ServiceError("hostname is required")
    now = time.time()
    db.execute(
        "INSERT INTO agents (hostname, last_seen, git_sha, protocol_version) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(hostname) DO UPDATE SET last_seen = excluded.last_seen,"
        " git_sha = excluded.git_sha, protocol_version = excluded.protocol_version",
        (hostname, now, str(body.get("git_sha") or ""), int(body.get("protocol_version") or 0)))
    agent_id = db.execute("SELECT id FROM agents WHERE hostname = ?", (hostname,)).fetchone()["id"]

    for d in body.get("displays") or []:
        connector = d.get("connector")
        if not isinstance(connector, str) or not connector:
            continue
        status = {k: d.get(k) for k in ("browser", "placement_ok", "override_active", "current", "errors", "screen_on")}
        db.execute(
            "INSERT INTO displays (agent_id, connector, name, width, height, orientation, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(agent_id, connector) DO UPDATE SET name = excluded.name,"
            " width = excluded.width, height = excluded.height,"
            " orientation = excluded.orientation, status = excluded.status",
            (agent_id, connector, str(d.get("name") or connector), int(d.get("width") or 0),
             int(d.get("height") or 0), str(d.get("orientation") or ""), json.dumps(status)))

    acks = [a for a in body.get("acks") or [] if isinstance(a, int)]
    if acks:
        db.execute(
            f"DELETE FROM commands WHERE agent_id = ? AND id IN ({','.join('?' * len(acks))})",
            (agent_id, *acks))
    db.execute("DELETE FROM commands WHERE created_at < ?", (now - COMMAND_TTL,))
    return agent_id


def pending_commands(db, agent_id):
    rows = db.execute("SELECT * FROM commands WHERE agent_id = ? ORDER BY id", (agent_id,)).fetchall()
    return [{"id": r["id"], "type": r["type"], "connector": r["connector"],
             "args": json.loads(r["args"])} for r in rows]
