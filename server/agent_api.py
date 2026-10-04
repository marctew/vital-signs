import hmac
import time

from flask import Blueprint, abort, current_app, jsonify, request

from shared.protocol import PROTOCOL_VERSION

from . import services
from .db import get_db

bp = Blueprint("agent_api", __name__, url_prefix="/api/agent")

MAX_SCREENSHOT_BYTES = 2 * 1024 * 1024


@bp.before_request
def check_token():
    token = current_app.config["VS"]["agent_token"]
    sent = request.headers.get("Authorization", "")
    if not token or not hmac.compare_digest(sent.encode(), f"Bearer {token}".encode()):
        abort(401)


@bp.post("/poll")
def poll():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="JSON body required"), 400
    db = get_db()
    try:
        agent_id = services.record_poll(db, body)
    except services.ServiceError as e:
        return jsonify(error=str(e)), e.status
    state, revision = services.desired_state(db, agent_id)
    commands = services.pending_commands(db, agent_id)
    db.commit()

    cfg = current_app.config["VS"]
    resp = {
        "protocol_version": PROTOCOL_VERSION,
        "server_sha": services.SERVER_SHA,
        "revision": revision,
        "poll_interval": 2,
        "screenshot_interval": cfg["screenshot_interval"],
        "commands": commands,
    }
    if body.get("revision") != revision:
        resp["displays"] = state
    return jsonify(resp)


@bp.post("/screenshot")
def screenshot():
    data = request.get_data(cache=False)
    if not data or len(data) > MAX_SCREENSHOT_BYTES:
        return jsonify(error="screenshot missing or too large"), 400
    if not data.startswith(b"\xff\xd8"):
        return jsonify(error="screenshot must be a JPEG"), 400
    db = get_db()
    row = db.execute(
        "SELECT d.id FROM displays d JOIN agents a ON a.id = d.agent_id"
        " WHERE a.hostname = ? AND d.connector = ?",
        (request.args.get("hostname", ""), request.args.get("connector", ""))).fetchone()
    if row is None:
        return jsonify(error="unknown display"), 404
    folder = current_app.config["SCREENSHOT_DIR"]
    tmp = folder / f"{row['id']}.tmp"
    tmp.write_bytes(data)
    tmp.replace(folder / f"{row['id']}.jpg")
    db.execute("UPDATE displays SET screenshot_at = ? WHERE id = ?", (time.time(), row["id"]))
    db.commit()
    return jsonify(ok=True)
