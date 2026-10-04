import hmac

from flask import Blueprint, current_app, jsonify, request

from . import services
from .db import get_db

bp = Blueprint("control_api", __name__, url_prefix="/api/v1")


@bp.before_request
def check_token():
    # Open on the LAN in v1. Setting control_token in the config turns auth on.
    token = current_app.config["VS"]["control_token"]
    if not token:
        return None
    sent = request.headers.get("Authorization", "")
    if not hmac.compare_digest(sent.encode(), f"Bearer {token}".encode()):
        return jsonify(error="unauthorized"), 401
    return None


@bp.errorhandler(services.ServiceError)
def service_error(e):
    return jsonify(error=str(e)), e.status


def _body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


@bp.get("/displays")
def displays():
    return jsonify(displays=services.list_displays(get_db()))


@bp.get("/displays/<ref>")
def display(ref):
    return jsonify(services.get_display(get_db(), ref))


@bp.get("/playlists")
def playlists():
    return jsonify(playlists=services.list_playlists(get_db()))


@bp.put("/displays/<ref>/assignment")
def assignment(ref):
    db = get_db()
    d = services.get_display(db, ref)
    body = _body()
    playlist_id = body.get("playlist_id")
    if playlist_id is None and body.get("playlist"):
        row = db.execute("SELECT id FROM playlists WHERE name = ?", (body["playlist"],)).fetchone()
        if row is None:
            raise services.ServiceError("No such playlist", 404)
        playlist_id = row["id"]
    services.assign(db, d["id"], playlist_id)
    return jsonify(services.get_display(db, d["id"]))


@bp.post("/displays/<ref>/override")
def push_override(ref):
    db = get_db()
    d = services.get_display(db, ref)
    body = _body()
    services.push_override(db, d["id"], body.get("url"), body.get("minutes", 5))
    return jsonify(services.get_display(db, d["id"]))


@bp.delete("/displays/<ref>/override")
def clear_override(ref):
    db = get_db()
    d = services.get_display(db, ref)
    services.clear_override(db, d["id"])
    return jsonify(services.get_display(db, d["id"]))


@bp.put("/displays/<ref>/power-schedule")
def power_schedule(ref):
    db = get_db()
    d = services.get_display(db, ref)
    body = request.get_json(silent=True)
    services.set_power_schedule(db, d["id"], body if isinstance(body, dict) and body else None)
    return jsonify(services.get_display(db, d["id"]))


@bp.post("/displays/<ref>/identify")
def identify(ref):
    db = get_db()
    d = services.get_display(db, ref)
    services.queue_command(db, d, "identify", {"seconds": _body().get("seconds", 5)})
    return jsonify(ok=True)


@bp.post("/displays/<ref>/reload")
def reload(ref):
    db = get_db()
    d = services.get_display(db, ref)
    services.queue_command(db, d, "reload")
    return jsonify(ok=True)


@bp.post("/displays/<ref>/next")
def next_item(ref):
    db = get_db()
    d = services.get_display(db, ref)
    services.queue_command(db, d, "next")
    return jsonify(ok=True)


@bp.post("/displays/<ref>/previous")
def previous_item(ref):
    db = get_db()
    d = services.get_display(db, ref)
    services.queue_command(db, d, "previous")
    return jsonify(ok=True)


@bp.post("/displays/<ref>/vnc")
def vnc(ref):
    db = get_db()
    d = services.get_display(db, ref)
    services.queue_command(db, d, "vnc")
    return jsonify(ok=True)
