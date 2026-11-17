"""The API a TRMNL e-ink device calls. Its paths are fixed by the firmware, so they sit at the site root.

    GET  /api/setup     first contact: the device gets its key (see eink.setup_reply for when)
    GET  /api/display   every wake: what to draw and how long to sleep
    POST /api/log       the device's own error log
    GET  /api/eink/image/<folder>/<name>.png   the picture a display reply named

A device proves itself with its MAC address (`ID`) and key (`Access-Token`) headers. Contract: shared/API.md.
"""
import sqlite3
import time

from flask import Blueprint, current_app, jsonify, request, send_file

from . import eink
from .db import get_db

bp = Blueprint("device_api", __name__)

BUSY_TIMEOUT_MS = 4000


@bp.before_request
def short_lock_wait():
    get_db().execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")


@bp.errorhandler(sqlite3.OperationalError)
def database_busy(e):
    """As for the Pi agent: a stalled disk gets a 503, and the device tries again on its next wake."""
    if "locked" not in str(e) and "busy" not in str(e):
        raise e
    get_db().rollback()
    return jsonify(error="database busy, try again"), 503


def _base_url():
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
    return f"{scheme}://{request.host}"


def _device():
    """The device making this request and whether its key is right: (row or None, bool)."""
    mac = eink.clean_mac(request.headers.get("ID"))
    if mac is None:
        return None, False
    dev = eink.get_device(get_db(), mac)
    return dev, bool(dev) and eink.check_key(dev, request.headers.get("Access-Token", ""))


@bp.get("/api/setup")
def setup():
    mac = eink.clean_mac(request.headers.get("ID"))
    if mac is None:
        return jsonify(status=400, message="ID header (MAC address) required"), 400
    body, status = eink.setup_reply(get_db(), mac, request.headers, time.time())
    return jsonify(body), status


@bp.get("/api/display")
def display():
    db = get_db()
    now = time.time()
    mac = eink.clean_mac(request.headers.get("ID"))
    if mac is None:
        return jsonify(error="ID header (MAC address) required"), 400
    dev = eink.get_device(db, mac)
    sent = request.headers.get("Access-Token", "")
    if dev is None:
        if not sent:
            return jsonify(error="unknown device"), 401
        # A device that already holds a key (set up elsewhere, or forgotten here): remember it as
        # pending with that key, so adopting it is all that is needed.
        dev = eink.register(db, mac, request.headers, now, api_key=sent)
    elif not eink.check_key(dev, sent):
        return jsonify(error="unauthorized"), 401
    eink.record_wake(db, dev, request.headers, now)
    dev = eink.get_device(db, mac)
    reply = eink.display_reply(db, dev, _base_url(), current_app.config["VS"]["data_dir"], now)
    db.commit()
    return jsonify(reply)


@bp.post("/api/log")
def log():
    dev, ok = _device()
    if not ok:
        return jsonify(error="unauthorized"), 401
    eink.add_logs(get_db(), dev, request.get_json(silent=True) or {}, time.time())
    return "", 204


@bp.get("/api/eink/image/<folder>/<filename>.png")
def image(folder, filename):
    dev, ok = _device()
    if not ok:
        return jsonify(error="unauthorized"), 401
    if dev["agent_id"] is None:
        allowed = f"pending-{dev['id']}"
    else:
        row = get_db().execute("SELECT id FROM displays WHERE agent_id = ? AND kind = 'eink'", (dev["agent_id"],)).fetchone()
        allowed = str(row["id"]) if row else ""
    path = eink.image_path(current_app.config["VS"]["data_dir"], folder, filename) if folder == allowed else None
    if path is None:
        return jsonify(error="not found"), 404
    return send_file(path, mimetype="image/png", max_age=0)
