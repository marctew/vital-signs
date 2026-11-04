"""/healthz: whether the server is up and its database answers. Needs no login.

It says nothing about displays or content. `busy` is always empty: the server keeps no long job a
restart would cut short (agents simply poll again), but scripts/update.sh reads it like every app's.
"""
import sqlite3

from flask import Blueprint, current_app

bp = Blueprint("health", __name__)


@bp.get("/healthz")
def healthz():
    try:
        conn = sqlite3.connect(current_app.config["DB_PATH"], timeout=2)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return {"ok": False, "busy": [], "error": f"database: {e}"}, 503
    return {"ok": True, "busy": []}
