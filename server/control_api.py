from flask import Blueprint, jsonify, request

from . import modules, services
from .db import get_db

bp = Blueprint("control_api", __name__, url_prefix="/api/v1")


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


@bp.get("/agents")
def agents():
    return jsonify(agents=services.list_agents(get_db()))


@bp.post("/agents/<ref>/restart-browser")
def agent_restart_browser(ref):
    db = get_db()
    services.queue_agent_command(db, services.get_agent(db, ref)["id"], "restart_browser")
    return jsonify(ok=True)


@bp.post("/agents/<ref>/reboot")
def agent_reboot(ref):
    db = get_db()
    services.queue_agent_command(db, services.get_agent(db, ref)["id"], "reboot")
    return jsonify(ok=True)


@bp.post("/boards/<ref>")
def board_push(ref):
    """Set what a Board module shows. `ref` is the board's name on the Content page, or its id."""
    body = request.get_json(silent=True)
    return jsonify(ok=True, shown=modules.push_board(get_db(), ref, body))


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


@bp.get("/schedules")
def schedules():
    return jsonify(schedules=services.list_schedules(get_db()))


@bp.put("/displays/<ref>/power-schedules")
def power_schedules(ref):
    db = get_db()
    d = services.get_display(db, ref)
    body = _body()
    ids = list(body.get("schedule_ids") or [])
    for name in body.get("schedules") or []:
        row = db.execute("SELECT id FROM power_schedules WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise services.ServiceError(f"No such schedule: {name}", 404)
        ids.append(row["id"])
    services.set_display_schedules(db, d["id"], ids)
    return jsonify(services.get_display(db, d["id"]))


@bp.post("/displays/<ref>/playlist-rules")
def playlist_rule_add(ref):
    db = get_db()
    d = services.get_display(db, ref)
    body = _body()
    schedule = db.execute("SELECT id FROM power_schedules WHERE id = ? OR name = ?",
                          (body.get("schedule_id"), body.get("schedule"))).fetchone()
    playlist = db.execute("SELECT id FROM playlists WHERE id = ? OR name = ?",
                          (body.get("playlist_id"), body.get("playlist"))).fetchone()
    if schedule is None or playlist is None:
        raise services.ServiceError("Unknown schedule or playlist", 404)
    services.add_playlist_rule(db, d["id"], schedule["id"], playlist["id"])
    return jsonify(services.get_display(db, d["id"]))


@bp.delete("/displays/<ref>/playlist-rules/<int:rule_id>")
def playlist_rule_delete(ref, rule_id):
    db = get_db()
    d = services.get_display(db, ref)
    services.delete_playlist_rule(db, d["id"], rule_id)
    return jsonify(services.get_display(db, d["id"]))


@bp.post("/displays/<ref>/power")
def power_override(ref):
    db = get_db()
    d = services.get_display(db, ref)
    on = _body().get("on")
    if not isinstance(on, bool):
        raise services.ServiceError('Body must be {"on": true} or {"on": false}')
    services.set_power_override(db, d["id"], on)
    return jsonify(services.get_display(db, d["id"]))


@bp.delete("/displays/<ref>/power")
def power_override_clear(ref):
    db = get_db()
    d = services.get_display(db, ref)
    services.set_power_override(db, d["id"], None)
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
