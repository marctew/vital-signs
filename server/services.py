"""Operations shared by the admin UI, the control API and the agent API."""
import hashlib
import json
import re
import time

from shared.protocol import COMMAND_TYPES, PROTOCOL_VERSION, TRANSITIONS, git_sha

ONLINE_SECONDS = 15
COMMAND_TTL = 300

SERVER_SHA = git_sha()

_EXTERNAL_URL = re.compile(r"^https?://\S+$")
_LOCAL_URL = re.compile(r"^/pages/[A-Za-z0-9][A-Za-z0-9_.-]*/\S*$")
_SPLIT_URL = re.compile(r"^/(split|rotate)/\d+/$")
_CLOCK_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ServiceError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def is_local_url(url):
    """True for URLs served by this server, which the agent resolves against its server_url."""
    return bool(_LOCAL_URL.match(url) or _SPLIT_URL.match(url))


def check_url(url):
    url = (url or "").strip()
    if not (_EXTERNAL_URL.match(url) or is_local_url(url)):
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
        "transition": row["transition"],
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
        "power_schedules": display_schedules(db, row["id"]),
        "power_override": row["power_override"] or None,
        "playlist_rules": playlist_rules(db, row["id"]),
        "screen_on": status.get("screen_on") is not False,
        "browser_memory_mb": status.get("browser_memory_mb"),
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


def set_display_transition(db, display_id, transition):
    if transition not in TRANSITIONS:
        raise ServiceError("Unknown transition")
    db.execute("UPDATE displays SET transition = ? WHERE id = ?", (transition, display_id))
    db.commit()


# --- agents (the Pis) -------------------------------------------------------

def _duration(seconds):
    days, rest = divmod(int(seconds), 86400)
    hours, minutes = rest // 3600, rest % 3600 // 60
    if days:
        return f"{days} d {hours} h"
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def health_facts(health):
    """A Pi's health readings as (label, value, is a warning) rows for display."""
    facts = []
    if "temperature_c" in health:
        hot = health["temperature_c"] >= 80
        facts.append(("Temperature", f"{health['temperature_c']:.0f} °C" + (" (running hot)" if hot else ""), hot))
    if "memory_used_percent" in health:
        total = health.get("memory_total_mb", 0) / 1024
        facts.append(("Memory", f"{health['memory_used_percent']}% of {total:.0f} GB used",
                      health["memory_used_percent"] >= 90))
    if "disk_used_percent" in health:
        facts.append(("Disk", f"{health['disk_used_percent']}% used", health["disk_used_percent"] >= 90))
    if "load" in health:
        facts.append(("Load", f"{health['load']:.2f}", False))
    if "network" in health:
        if health["network"] == "wifi":
            signal = health.get("wifi_signal_dbm")
            weak = signal is not None and signal < -75
            text = "Wi-Fi" + (f", {signal} dBm" + (" (weak)" if weak else "") if signal is not None else "")
            facts.append(("Network", text, weak))
        else:
            facts.append(("Network", "Ethernet", False))
    if health.get("under_voltage") or health.get("throttled"):
        facts.append(("Power", "under-voltage now: check the power supply" if health.get("under_voltage")
                      else "being slowed down now", True))
    elif health.get("under_voltage_seen") or health.get("throttled_seen"):
        facts.append(("Power", "under-voltage or slow-down since boot", True))
    elif "under_voltage" in health:
        facts.append(("Power", "OK", False))
    if "uptime_s" in health:
        facts.append(("Up for", _duration(health["uptime_s"]), False))
    return [{"label": label, "value": value, "warn": warn} for label, value, warn in facts]


def list_agents(db):
    now = time.time()
    agents = []
    for row in db.execute("SELECT * FROM agents ORDER BY hostname").fetchall():
        health = json.loads(row["health"] or "{}")
        agents.append({
            "id": row["id"], "hostname": row["hostname"], "online": now - row["last_seen"] < ONLINE_SECONDS,
            "last_seen": row["last_seen"], "git_sha": row["git_sha"], "protocol_version": row["protocol_version"],
            "version_drift": row["git_sha"] != SERVER_SHA or row["protocol_version"] != PROTOCOL_VERSION,
            "health": health, "facts": health_facts(health),
            "displays": [r["name"] for r in db.execute(
                "SELECT name FROM displays WHERE agent_id = ? ORDER BY name", (row["id"],))],
        })
    return agents


def get_agent(db, ref):
    """Look an agent up by numeric id or hostname."""
    for agent in list_agents(db):
        if str(agent["id"]) == str(ref) or agent["hostname"] == ref:
            return agent
    raise ServiceError(f"No such agent: {ref}", 404)


def queue_agent_command(db, agent_id, type_):
    """Queue a command for the Pi as a whole (every display, or the machine itself)."""
    if type_ not in COMMAND_TYPES:
        raise ServiceError(f"Unknown command: {type_}")
    db.execute("INSERT INTO commands (agent_id, connector, type, args, created_at) VALUES (?, NULL, ?, '{}', ?)",
               (agent_id, type_, time.time()))
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


# --- screen power schedules -------------------------------------------------
#
# A schedule is one named on-period: an on time, an off time and the days it
# starts on (Monday is 0). A display can have several; its screen is on
# whenever any of them is, and always on when it has none. The agent applies
# them in the Pi's local time, so they keep working offline.

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _schedule_dict(row):
    return {"id": row["id"], "name": row["name"], "on": row["on_time"], "off": row["off_time"],
            "days": json.loads(row["days"])}


def list_schedules(db):
    schedules = []
    for row in db.execute("SELECT * FROM power_schedules ORDER BY on_time, name").fetchall():
        s = _schedule_dict(row)
        s["displays"] = [r["name"] for r in db.execute(
            "SELECT d.name FROM displays d WHERE d.id IN"
            " (SELECT display_id FROM display_schedules WHERE schedule_id = :s"
            "  UNION SELECT display_id FROM playlist_rules WHERE schedule_id = :s)"
            " ORDER BY d.name", {"s": row["id"]})]
        schedules.append(s)
    return schedules


def get_schedule(db, schedule_id):
    row = db.execute("SELECT * FROM power_schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        raise ServiceError("No such schedule", 404)
    return _schedule_dict(row)


def save_schedule(db, schedule_id, name, on, off, days):
    """Create a schedule (schedule_id None) or update one. Returns its id."""
    name, on, off = (name or "").strip(), str(on or ""), str(off or "")
    if not name:
        raise ServiceError("Name is required")
    if not (_CLOCK_TIME.match(on) and _CLOCK_TIME.match(off)):
        raise ServiceError("On and off must be times like 07:30")
    if on == off:
        raise ServiceError("On and off times must differ")
    days = sorted({int(d) for d in days or [] if str(d) in list("0123456")})
    if not days:
        raise ServiceError("Choose at least one day")
    if db.execute("SELECT 1 FROM power_schedules WHERE name = ? AND id IS NOT ?", (name, schedule_id)).fetchone():
        raise ServiceError("A schedule with that name already exists")
    values = (name, on, off, json.dumps(days))
    if schedule_id is None:
        schedule_id = db.execute(
            "INSERT INTO power_schedules (name, on_time, off_time, days) VALUES (?, ?, ?, ?)", values).lastrowid
    else:
        get_schedule(db, schedule_id)
        db.execute("UPDATE power_schedules SET name = ?, on_time = ?, off_time = ?, days = ? WHERE id = ?",
                   (*values, schedule_id))
    db.commit()
    return schedule_id


def delete_schedule(db, schedule_id):
    db.execute("DELETE FROM power_schedules WHERE id = ?", (schedule_id,))
    db.commit()


def display_schedules(db, display_id):
    rows = db.execute(
        "SELECT s.* FROM display_schedules ds JOIN power_schedules s ON s.id = ds.schedule_id"
        " WHERE ds.display_id = ? ORDER BY s.on_time, s.name", (display_id,)).fetchall()
    return [_schedule_dict(r) for r in rows]


def set_display_schedules(db, display_id, schedule_ids):
    """Assign exactly these schedules to a display. An empty list means always on."""
    try:
        ids = sorted({int(i) for i in schedule_ids or []})
    except (TypeError, ValueError):
        raise ServiceError("schedule ids must be numbers")
    for schedule_id in ids:
        get_schedule(db, schedule_id)
    db.execute("DELETE FROM display_schedules WHERE display_id = ?", (display_id,))
    db.executemany("INSERT INTO display_schedules (display_id, schedule_id) VALUES (?, ?)",
                   [(display_id, i) for i in ids])
    db.commit()


def copy_display_schedules(db, display_id, source_id):
    """Give a display exactly the schedules another display has (including none)."""
    if source_id == display_id:
        raise ServiceError("Choose another display to copy from")
    set_display_schedules(db, display_id, [s["id"] for s in display_schedules(db, source_id)])


def playlist_rules(db, display_id):
    """Playlists a display shows instead of its usual one while a schedule is active.

    Earlier rules win when two are active at once.
    """
    rows = db.execute(
        "SELECT r.id, r.playlist_id, p.name AS playlist, s.* FROM playlist_rules r"
        " JOIN power_schedules s ON s.id = r.schedule_id JOIN playlists p ON p.id = r.playlist_id"
        " WHERE r.display_id = ? ORDER BY r.id", (display_id,)).fetchall()
    return [{"id": r["id"], "playlist_id": r["playlist_id"], "playlist": r["playlist"],
             "schedule": r["name"], "on": r["on_time"], "off": r["off_time"],
             "days": json.loads(r["days"])} for r in rows]


def add_playlist_rule(db, display_id, schedule_id, playlist_id):
    get_schedule(db, schedule_id)
    if not db.execute("SELECT 1 FROM playlists WHERE id = ?", (playlist_id,)).fetchone():
        raise ServiceError("No such playlist", 404)
    db.execute("INSERT INTO playlist_rules (display_id, schedule_id, playlist_id) VALUES (?, ?, ?)",
               (display_id, schedule_id, playlist_id))
    db.commit()


def delete_playlist_rule(db, display_id, rule_id):
    db.execute("DELETE FROM playlist_rules WHERE id = ? AND display_id = ?", (rule_id, display_id))
    db.commit()


def set_power_override(db, display_id, on):
    """Force a screen on (True) or off (False) until its schedule next changes state.

    None goes back to the schedule now. The agent ends the override itself at the
    next scheduled change and reports it, which clears it here. A display with
    no schedules has no next change, so the override stays until it is cleared.
    """
    if on is None:
        db.execute("UPDATE displays SET power_override = '' WHERE id = ?", (display_id,))
    else:
        db.execute("UPDATE displays SET power_override = ?, power_override_id = power_override_id + 1"
                   " WHERE id = ?", ("on" if on else "off", display_id))
    db.commit()


def week_segments(schedules):
    """Lay schedules out on a week for the timeline: seven lists (Monday first) of on-periods.

    Each period has `left` and `width` as percentages of a day, and a `label`.
    A period that runs past midnight is split across the two days it touches.
    No schedules means the screen is always on.
    """
    week = [[] for _ in range(7)]
    if not schedules:
        return [[{"left": 0, "width": 100, "label": "Always on"}] for _ in range(7)]

    def minutes(clock):
        return int(clock[:2]) * 60 + int(clock[3:])

    def add(day, start, end, label):
        if end > start:
            week[day % 7].append({"left": round(start / 14.4, 3), "width": round((end - start) / 14.4, 3),
                                  "label": label})

    for s in schedules:
        on, off = minutes(s["on"]), minutes(s["off"])
        label = f"{s['name']}: {s['on']} to {s['off']}"
        for day in s["days"]:
            if on < off:
                add(day, on, off, label)
            else:
                add(day, on, 1440, label)
                add(day + 1, 0, off, label)
    return week


def queue_command(db, display, type_, args=None):
    if type_ not in COMMAND_TYPES:
        raise ServiceError(f"Unknown command: {type_}")
    db.execute(
        "INSERT INTO commands (agent_id, connector, type, args, created_at) VALUES (?, ?, ?, ?, ?)",
        (display["agent_id"], display["connector"], type_, json.dumps(args or {}), time.time()))
    db.commit()


def queue_update(db):
    """Ask every agent to update itself. Returns how many were asked."""
    agents = db.execute("SELECT id FROM agents").fetchall()
    for agent in agents:
        db.execute("INSERT INTO commands (agent_id, connector, type, args, created_at) VALUES (?, NULL, 'update', '{}', ?)",
                   (agent["id"], time.time()))
    db.commit()
    return len(agents)


# --- rotations: a playlist cycling inside one box -----------------------------
#
# Every playlist gets a content item of kind "rotation" whose page (/rotate/<id>/)
# cycles through the playlist's items. Placing it in a split screen makes that
# cell rotate. They are created and removed here, never by hand.

def sync_rotations(db):
    playlists = {p["id"]: p["name"] for p in db.execute("SELECT id, name FROM playlists")}
    existing = {}
    for row in db.execute("SELECT id, name, config FROM content_items WHERE kind = 'rotation'").fetchall():
        playlist_id = json.loads(row["config"] or "{}").get("playlist_id")
        if playlist_id not in playlists or playlist_id in existing:
            db.execute("DELETE FROM content_items WHERE id = ?", (row["id"],))
            continue
        existing[playlist_id] = row["id"]
        wanted = f"{playlists[playlist_id]} (rotating)"
        if row["name"] != wanted:
            db.execute("UPDATE content_items SET name = ? WHERE id = ?", (wanted, row["id"]))
    for playlist_id, name in playlists.items():
        if playlist_id not in existing:
            new_id = db.execute("INSERT INTO content_items (name, url, kind, config) VALUES (?, '', 'rotation', ?)",
                                (f"{name} (rotating)", json.dumps({"playlist_id": playlist_id}))).lastrowid
            db.execute("UPDATE content_items SET url = ? WHERE id = ?", (f"/rotate/{new_id}/", new_id))
    db.commit()


def get_rotation(db, content_id):
    """A rotation with the items it cycles through, or None if the content item is not one."""
    row = db.execute("SELECT * FROM content_items WHERE id = ? AND kind = 'rotation'", (content_id,)).fetchone()
    if row is None:
        return None
    playlist_id = json.loads(row["config"] or "{}").get("playlist_id")
    # Only plain pages and modules rotate: a split or another rotation inside one is left out.
    items = db.execute(
        "SELECT c.name, c.url, c.zoom, c.refresh_interval, pi.duration FROM playlist_items pi"
        " JOIN content_items c ON c.id = pi.content_id"
        " WHERE pi.playlist_id = ? AND c.kind IN ('url', 'module') ORDER BY pi.position, pi.id", (playlist_id,)).fetchall()
    return {"id": row["id"], "name": row["name"], "playlist_id": playlist_id, "items": [dict(i) for i in items]}


# --- split screens ----------------------------------------------------------

# A split screen places content on a grid: 2 columns by 4 rows for portrait
# screens or 4 by 2 for landscape. A pane covers a rectangle of cells.
SPLIT_GRIDS = {"2x4": (2, 4), "4x2": (4, 2)}


def get_split(db, content_id):
    """A split screen with its panes, or None if the content item is not one."""
    item = db.execute("SELECT * FROM content_items WHERE id = ? AND kind = 'split'", (content_id,)).fetchone()
    if item is None:
        return None
    panes = db.execute(
        "SELECT sp.col, sp.row, sp.col_span, sp.row_span, sp.autohide, c.id AS content_id, c.name, c.url, c.zoom,"
        " c.refresh_interval FROM split_panes sp JOIN content_items c ON c.id = sp.content_id"
        " WHERE sp.split_id = ? ORDER BY sp.row, sp.col, sp.id", (content_id,)).fetchall()
    grid = item["split_grid"] if item["split_grid"] in SPLIT_GRIDS else "2x4"
    cols, rows = SPLIT_GRIDS[grid]
    return {"id": item["id"], "name": item["name"], "grid": grid, "cols": cols, "rows": rows,
            "panes": [dict(p) for p in panes]}


def save_split(db, content_id, name, grid, panes):
    """Create a split screen (content_id None) or update one. Returns its content id.

    `panes` is a list of {"content_id", "col", "row", "col_span", "row_span"} in grid cells, with an
    optional "autohide": the pane disappears while its module reports it has nothing to show.
    """
    name = (name or "").strip()
    if not name:
        raise ServiceError("Name is required")
    if grid not in SPLIT_GRIDS:
        raise ServiceError("Unknown grid")
    cols, rows = SPLIT_GRIDS[grid]
    placed = []
    taken = set()
    for pane in panes:
        try:
            child_id, col, row, col_span, row_span = (int(pane[k]) for k in
                                                      ("content_id", "col", "row", "col_span", "row_span"))
        except (KeyError, TypeError, ValueError):
            raise ServiceError("Bad pane position")
        child = db.execute("SELECT kind FROM content_items WHERE id = ?", (child_id,)).fetchone()
        if child is None or child["kind"] == "split":
            raise ServiceError("A split screen cannot contain another split screen")
        if col < 0 or row < 0 or col_span < 1 or row_span < 1 or col + col_span > cols or row + row_span > rows:
            raise ServiceError("A pane is outside the grid")
        cells = {(c, r) for c in range(col, col + col_span) for r in range(row, row + row_span)}
        if cells & taken:
            raise ServiceError("Two panes overlap")
        taken |= cells
        placed.append((child_id, col, row, col_span, row_span, 1 if str(pane.get("autohide", "")) in ("1", "True", "true", "on") else 0))
    if not placed:
        raise ServiceError("Place at least one page on the grid")
    if content_id is None:
        content_id = db.execute("INSERT INTO content_items (name, url, kind, split_grid)"
                                " VALUES (?, '', 'split', ?)", (name, grid)).lastrowid
        db.execute("UPDATE content_items SET url = ? WHERE id = ?", (f"/split/{content_id}/", content_id))
    else:
        if get_split(db, content_id) is None:
            raise ServiceError("No such split screen", 404)
        db.execute("UPDATE content_items SET name = ?, split_grid = ? WHERE id = ?", (name, grid, content_id))
        db.execute("DELETE FROM split_panes WHERE split_id = ?", (content_id,))
    db.executemany("INSERT INTO split_panes (split_id, content_id, col, row, col_span, row_span, autohide)"
                   " VALUES (?, ?, ?, ?, ?, ?, ?)", [(content_id, *p) for p in placed])
    db.commit()
    return content_id


def list_playlists(db):
    rows = db.execute(
        "SELECT p.id, p.name, COUNT(pi.id) AS item_count FROM playlists p"
        " LEFT JOIN playlist_items pi ON pi.playlist_id = p.id GROUP BY p.id ORDER BY p.name").fetchall()
    return [dict(r) for r in rows]


# --- agent side -------------------------------------------------------------

def _playlist_items(db, playlist_id):
    rows = db.execute(
        "SELECT pi.id, pi.duration, c.id AS content_id, c.name, c.url, c.zoom, c.refresh_interval, c.css"
        " FROM playlist_items pi JOIN content_items c ON c.id = pi.content_id"
        " WHERE pi.playlist_id = ? ORDER BY pi.position, pi.id", (playlist_id,)).fetchall()
    return [{
        "id": i["id"],
        "content_id": i["content_id"],
        "name": i["name"],
        "url": i["url"],
        "local": is_local_url(i["url"]),
        "duration": i["duration"],
        "zoom": i["zoom"],
        "refresh": i["refresh_interval"],
        "css": i["css"],
    } for i in rows]


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
        assigned = db.execute("SELECT playlist_id FROM assignments WHERE display_id = ?", (d["id"],)).fetchone()
        ov = db.execute("SELECT * FROM overrides WHERE display_id = ? ORDER BY id DESC LIMIT 1",
                        (d["id"],)).fetchone()
        state[d["connector"]] = {
            "display_zoom": d["zoom"],
            "transition": d["transition"] if d["transition"] in TRANSITIONS else "fade",
            "power": [{"on": s["on"], "off": s["off"], "days": s["days"]}
                      for s in display_schedules(db, d["id"])] or None,
            "power_override": {"id": d["power_override_id"], "on": d["power_override"] == "on"}
            if d["power_override"] else None,
            "items": _playlist_items(db, assigned["playlist_id"]) if assigned else [],
            "scheduled": [{"on": r["on"], "off": r["off"], "days": r["days"], "playlist": r["playlist"],
                           "items": _playlist_items(db, r["playlist_id"])}
                          for r in playlist_rules(db, d["id"])],
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
    if isinstance(body.get("health"), dict):
        db.execute("UPDATE agents SET health = ? WHERE hostname = ?", (json.dumps(body["health"]), hostname))
    agent_id = db.execute("SELECT id FROM agents WHERE hostname = ?", (hostname,)).fetchone()["id"]

    for d in body.get("displays") or []:
        connector = d.get("connector")
        if not isinstance(connector, str) or not connector:
            continue
        status = {k: d.get(k) for k in ("browser", "placement_ok", "override_active", "current", "errors", "screen_on", "browser_memory_mb")}
        db.execute(
            "INSERT INTO displays (agent_id, connector, name, width, height, orientation, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(agent_id, connector) DO UPDATE SET name = excluded.name,"
            " width = excluded.width, height = excluded.height,"
            " orientation = excluded.orientation, status = excluded.status",
            (agent_id, connector, str(d.get("name") or connector), int(d.get("width") or 0),
             int(d.get("height") or 0), str(d.get("orientation") or ""), json.dumps(status)))

        # The agent ends a power override at the next scheduled change and says which one.
        if isinstance(d.get("power_override_done"), int):
            db.execute("UPDATE displays SET power_override = '' WHERE agent_id = ? AND connector = ?"
                       " AND power_override_id = ?", (agent_id, connector, d["power_override_done"]))

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
