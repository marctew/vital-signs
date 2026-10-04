import json
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY,
    hostname TEXT NOT NULL UNIQUE,
    last_seen REAL NOT NULL DEFAULT 0,
    git_sha TEXT NOT NULL DEFAULT '',
    protocol_version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS displays (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    connector TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    orientation TEXT NOT NULL DEFAULT '',
    zoom REAL NOT NULL DEFAULT 1.0,
    status TEXT NOT NULL DEFAULT '{}',
    screenshot_at REAL NOT NULL DEFAULT 0,
    power_schedule TEXT NOT NULL DEFAULT '',
    power_override TEXT NOT NULL DEFAULT '',
    power_override_id INTEGER NOT NULL DEFAULT 0,
    UNIQUE (agent_id, connector)
);
CREATE TABLE IF NOT EXISTS content_items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    zoom REAL NOT NULL DEFAULT 1.0,
    refresh_interval INTEGER NOT NULL DEFAULT 0,
    css TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'url',
    split_direction TEXT NOT NULL DEFAULT 'auto',
    split_grid TEXT NOT NULL DEFAULT '2x4'
);
CREATE TABLE IF NOT EXISTS split_panes (
    id INTEGER PRIMARY KEY,
    split_id INTEGER NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
    content_id INTEGER NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 1,
    col INTEGER NOT NULL DEFAULT 0,
    row INTEGER NOT NULL DEFAULT 0,
    col_span INTEGER NOT NULL DEFAULT 1,
    row_span INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS playlist_items (
    id INTEGER PRIMARY KEY,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    content_id INTEGER NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    duration INTEGER NOT NULL DEFAULT 30
);
CREATE TABLE IF NOT EXISTS assignments (
    display_id INTEGER PRIMARY KEY REFERENCES displays(id) ON DELETE CASCADE,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_id INTEGER NOT NULL REFERENCES displays(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS power_schedules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    on_time TEXT NOT NULL,
    off_time TEXT NOT NULL,
    days TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS display_schedules (
    display_id INTEGER NOT NULL REFERENCES displays(id) ON DELETE CASCADE,
    schedule_id INTEGER NOT NULL REFERENCES power_schedules(id) ON DELETE CASCADE,
    PRIMARY KEY (display_id, schedule_id)
);
CREATE TABLE IF NOT EXISTS playlist_rules (
    id INTEGER PRIMARY KEY,
    display_id INTEGER NOT NULL REFERENCES displays(id) ON DELETE CASCADE,
    schedule_id INTEGER NOT NULL REFERENCES power_schedules(id) ON DELETE CASCADE,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    connector TEXT,
    type TEXT NOT NULL,
    args TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
"""


def connect(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate_splits_to_grid(conn):
    """Splits used to be a stack or row of panes with relative sizes. Place them on the grid."""
    conn.execute("ALTER TABLE content_items ADD COLUMN split_grid TEXT NOT NULL DEFAULT '2x4'")
    for column in ("col INTEGER NOT NULL DEFAULT 0", "row INTEGER NOT NULL DEFAULT 0",
                   "col_span INTEGER NOT NULL DEFAULT 1", "row_span INTEGER NOT NULL DEFAULT 1"):
        conn.execute(f"ALTER TABLE split_panes ADD COLUMN {column}")
    for split in conn.execute("SELECT id, split_direction FROM content_items WHERE kind = 'split'").fetchall():
        panes = conn.execute("SELECT id, size FROM split_panes WHERE split_id = ? ORDER BY position, id",
                             (split["id"],)).fetchall()[:4]
        if not panes:
            continue
        # Share the four cells along the long axis in proportion to the old sizes.
        total = sum(p["size"] for p in panes)
        spans = [max(1, round(4 * p["size"] / total)) for p in panes]
        while sum(spans) > 4:
            spans[spans.index(max(spans))] -= 1
        while sum(spans) < 4:
            spans[spans.index(min(spans))] += 1
        side_by_side = split["split_direction"] == "columns"
        conn.execute("UPDATE content_items SET split_grid = ? WHERE id = ?",
                     ("4x2" if side_by_side else "2x4", split["id"]))
        start = 0
        for pane, span in zip(panes, spans):
            if side_by_side:
                conn.execute("UPDATE split_panes SET col = ?, row = 0, col_span = ?, row_span = 2 WHERE id = ?",
                             (start, span, pane["id"]))
            else:
                conn.execute("UPDATE split_panes SET col = 0, row = ?, col_span = 2, row_span = ? WHERE id = ?",
                             (start, span, pane["id"]))
            start += span


def migrate(conn):
    """Bring a database created by an older version up to the current schema."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(displays)")}
    if "power_schedule" not in columns:
        conn.execute("ALTER TABLE displays ADD COLUMN power_schedule TEXT NOT NULL DEFAULT ''")
    if "kind" not in {row["name"] for row in conn.execute("PRAGMA table_info(content_items)")}:
        conn.execute("ALTER TABLE content_items ADD COLUMN kind TEXT NOT NULL DEFAULT 'url'")
        conn.execute("ALTER TABLE content_items ADD COLUMN split_direction TEXT NOT NULL DEFAULT 'auto'")
    if "col" not in {row["name"] for row in conn.execute("PRAGMA table_info(split_panes)")}:
        _migrate_splits_to_grid(conn)
    if "power_override" not in columns:
        conn.execute("ALTER TABLE displays ADD COLUMN power_override TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE displays ADD COLUMN power_override_id INTEGER NOT NULL DEFAULT 0")

    # displays.power_schedule held one schedule per display. Schedules are now
    # named, shared and assigned, so turn each old one into a named schedule.
    for row in conn.execute("SELECT id, power_schedule FROM displays WHERE power_schedule != ''").fetchall():
        try:
            old = json.loads(row["power_schedule"])
            on, off, days = old["on"], old["off"], json.dumps(sorted(old["days"]))
        except (ValueError, KeyError, TypeError):
            on = None
        if on:
            found = conn.execute("SELECT id FROM power_schedules WHERE on_time = ? AND off_time = ? AND days = ?",
                                 (on, off, days)).fetchone()
            if found:
                schedule_id = found["id"]
            else:
                name, n = f"{on} to {off}", 1
                while conn.execute("SELECT 1 FROM power_schedules WHERE name = ?", (name,)).fetchone():
                    n += 1
                    name = f"{on} to {off} ({n})"
                schedule_id = conn.execute(
                    "INSERT INTO power_schedules (name, on_time, off_time, days) VALUES (?, ?, ?, ?)",
                    (name, on, off, days)).lastrowid
            conn.execute("INSERT OR IGNORE INTO display_schedules (display_id, schedule_id) VALUES (?, ?)",
                         (row["id"], schedule_id))
        conn.execute("UPDATE displays SET power_schedule = '' WHERE id = ?", (row["id"],))


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def init_app(app):
    conn = connect(app.config["DB_PATH"])
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
    conn.close()

    @app.teardown_appcontext
    def close_db(exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()
