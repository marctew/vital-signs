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
    UNIQUE (agent_id, connector)
);
CREATE TABLE IF NOT EXISTS content_items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    zoom REAL NOT NULL DEFAULT 1.0,
    refresh_interval INTEGER NOT NULL DEFAULT 0,
    css TEXT NOT NULL DEFAULT ''
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


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def init_app(app):
    conn = connect(app.config["DB_PATH"])
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    @app.teardown_appcontext
    def close_db(exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()
