"""Server-rendered admin UI. Mutations go through services, same as the control API."""
import json

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from shared.protocol import start_update

from . import localpages, modules, services
from .db import get_db

bp = Blueprint("ui", __name__)


@bp.app_context_processor
def template_helpers():
    return {"week_segments": services.week_segments, "day_names": services.DAY_NAMES,
            "transitions": (("none", "None (switch instantly)"), ("fade", "Fade through black"),
                            ("slow", "Slow fade through black")),
            "server_sha": services.SERVER_SHA}


@bp.errorhandler(services.ServiceError)
def service_error(e):
    flash(str(e), "error")
    return redirect(request.referrer or url_for("ui.dashboard"))


def _int(value, default=0, minimum=0):
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _float(value, default=1.0):
    try:
        return min(10.0, max(0.1, float(value)))
    except (TypeError, ValueError):
        return default


# --- dashboard --------------------------------------------------------------

@bp.get("/")
def dashboard():
    db = get_db()
    return render_template("dashboard.html", displays=services.list_displays(db), agents=services.list_agents(db))


@bp.get("/displays/<int:display_id>")
def display(display_id):
    db = get_db()
    try:
        d = services.get_display(db, display_id)
    except services.ServiceError:
        abort(404)
    content = db.execute("SELECT name, url FROM content_items ORDER BY name").fetchall()
    return render_template("display.html", d=d, displays=services.list_displays(db),
                           schedules=services.list_schedules(db),
                           playlists=services.list_playlists(db), content=content)


def _back(display_id):
    """After an action on a display, return to the page it was pressed on."""
    return redirect(request.referrer or url_for("ui.display", display_id=display_id))


@bp.post("/displays/<int:display_id>/assign")
def assign(display_id):
    db = get_db()
    playlist_id = request.form.get("playlist_id")
    services.assign(db, services.get_display(db, display_id)["id"],
                    int(playlist_id) if playlist_id else None)
    return _back(display_id)


@bp.post("/displays/<int:display_id>/override")
def push_override(display_id):
    db = get_db()
    d = services.get_display(db, display_id)
    services.push_override(db, d["id"], request.form.get("url"), request.form.get("minutes"))
    return _back(display_id)


@bp.post("/displays/<int:display_id>/override/clear")
def clear_override(display_id):
    db = get_db()
    services.clear_override(db, services.get_display(db, display_id)["id"])
    return _back(display_id)


@bp.post("/displays/<int:display_id>/command/<type_>")
def command(display_id, type_):
    db = get_db()
    services.queue_command(db, services.get_display(db, display_id), type_)
    flash(f"Sent {type_}", "ok")
    return _back(display_id)


@bp.post("/displays/<int:display_id>/zoom")
def display_zoom(display_id):
    db = get_db()
    services.set_display_zoom(db, services.get_display(db, display_id)["id"], request.form.get("zoom"))
    return _back(display_id)


@bp.post("/displays/<int:display_id>/power")
def power_schedules(display_id):
    db = get_db()
    d = services.get_display(db, display_id)
    services.set_display_schedules(db, d["id"], request.form.getlist("schedule_ids"))
    flash(f"Screen schedules saved for {d['name']}", "ok")
    return _back(display_id)


@bp.post("/displays/<int:display_id>/playlist-rules")
def playlist_rule_add(display_id):
    db = get_db()
    d = services.get_display(db, display_id)
    services.add_playlist_rule(db, d["id"], _int(request.form.get("schedule_id")),
                               _int(request.form.get("playlist_id")))
    return _back(display_id)


@bp.post("/displays/<int:display_id>/playlist-rules/<int:rule_id>/delete")
def playlist_rule_delete(display_id, rule_id):
    db = get_db()
    services.delete_playlist_rule(db, services.get_display(db, display_id)["id"], rule_id)
    return _back(display_id)


@bp.post("/displays/<int:display_id>/power/override")
def power_override(display_id):
    db = get_db()
    d = services.get_display(db, display_id)
    state = request.form.get("state")
    services.set_power_override(db, d["id"], {"on": True, "off": False}.get(state))
    return _back(display_id)


@bp.post("/displays/<int:display_id>/power/copy")
def power_schedules_copy(display_id):
    db = get_db()
    d = services.get_display(db, display_id)
    source = services.get_display(db, request.form.get("from_id") or "0")
    services.copy_display_schedules(db, d["id"], source["id"])
    flash(f"Copied the screen schedules from {source['name']} to {d['name']}", "ok")
    return _back(display_id)


@bp.post("/update")
def update_all():
    """Update the server and every agent from the repo."""
    count = services.queue_update(get_db())
    start_update(current_app.config["VS"]["data_dir"] / "update.log")
    flash(f"Update started on the server and {count} agent(s). Anything with changes to pick up "
          "restarts itself in the next minute; the page may be briefly unavailable.", "ok")
    return redirect(url_for("ui.dashboard"))


@bp.post("/displays/<int:display_id>/transition")
def display_transition(display_id):
    db = get_db()
    services.set_display_transition(db, services.get_display(db, display_id)["id"], request.form.get("transition"))
    return _back(display_id)


@bp.post("/agents/<int:agent_id>/command/<type_>")
def agent_command(agent_id, type_):
    if type_ not in ("restart_browser", "reboot"):
        abort(404)
    db = get_db()
    agent = services.get_agent(db, agent_id)
    services.queue_agent_command(db, agent["id"], type_)
    flash(f"{agent['hostname']} will restart now; its screens go dark for up to a minute." if type_ == "reboot"
          else f"Restarting the browsers on {agent['hostname']}.", "ok")
    return redirect(url_for("ui.dashboard"))


@bp.post("/agents/<int:agent_id>/delete")
def delete_agent(agent_id):
    db = get_db()
    db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    db.commit()
    return redirect(url_for("ui.dashboard"))


# --- content ----------------------------------------------------------------

def _content_form():
    name = request.form.get("name", "").strip()
    url = services.check_url(request.form.get("url"))
    if not name:
        raise services.ServiceError("Name is required")
    return (name, url, _float(request.form.get("zoom")),
            _int(request.form.get("refresh_interval")), request.form.get("css", "").strip())


def _content_page(item=None):
    db = get_db()
    localpages.sync_content(db)
    items = db.execute("SELECT * FROM content_items ORDER BY name").fetchall()
    splits = {i["id"]: services.get_split(db, i["id"]) for i in items if i["kind"] == "split"}
    available = modules.list_modules()
    placeable = [i for i in items if i["kind"] != "split"]
    # The module form shows for ?module=<name> (adding one) or when editing a module item.
    module = available.get(item["module"] if item and item["kind"] == "module" else request.args.get("module"))
    previews = {}
    for path in current_app.config["PREVIEW_DIR"].glob("*.jpg"):
        if path.stem.isdigit():
            previews[int(path.stem)] = int(path.stat().st_mtime)
    return render_template(
        "content.html", items=items, item=item, pages=localpages.list_pages(), previews=previews,
        plain_items=placeable, plain_names={i["id"]: i["name"] for i in placeable},
        split_names={k: [p["name"] for p in v["panes"]] for k, v in splits.items()},
        split=splits.get(item["id"]) if item else None,
        modules=available, module=module,
        module_config=json.loads(item["config"] or "{}") if item and item["kind"] == "module" else {})


@bp.post("/content/module/<module_name>")
@bp.post("/content/<int:item_id>/module/<module_name>")
def module_save(module_name, item_id=None):
    modules.save_module(get_db(), item_id, module_name, request.form.get("name"), request.form)
    return redirect(url_for("ui.content"))


@bp.get("/content")
def content():
    return _content_page()


@bp.post("/content/split")
@bp.post("/content/<int:item_id>/split")
def split_save(item_id=None):
    form = request.form
    keys = ("content_id", "col", "row", "col_span", "row_span")
    panes = [dict(zip(keys, values)) for values in zip(*(form.getlist(k) for k in keys))]
    services.save_split(get_db(), item_id, form.get("name"), form.get("grid"), panes)
    return redirect(url_for("ui.content"))


@bp.post("/content")
def content_create():
    db = get_db()
    db.execute("INSERT INTO content_items (name, url, zoom, refresh_interval, css) VALUES (?, ?, ?, ?, ?)",
               _content_form())
    db.commit()
    return redirect(url_for("ui.content"))


@bp.get("/content/<int:item_id>")
def content_edit(item_id):
    item = get_db().execute("SELECT * FROM content_items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    return _content_page(item)


@bp.post("/content/<int:item_id>")
def content_update(item_id):
    db = get_db()
    db.execute("UPDATE content_items SET name = ?, url = ?, zoom = ?, refresh_interval = ?, css = ?"
               " WHERE id = ? AND kind = 'url'", (*_content_form(), item_id))
    db.commit()
    return redirect(url_for("ui.content"))


@bp.post("/content/<int:item_id>/delete")
def content_delete(item_id):
    db = get_db()
    db.execute("DELETE FROM content_items WHERE id = ?", (item_id,))
    db.commit()
    (current_app.config["PREVIEW_DIR"] / f"{item_id}.jpg").unlink(missing_ok=True)
    return redirect(url_for("ui.content"))


# --- playlists --------------------------------------------------------------

@bp.get("/playlists")
def playlists():
    return render_template("playlists.html", playlists=services.list_playlists(get_db()))


@bp.post("/playlists")
def playlist_create():
    db = get_db()
    name = request.form.get("name", "").strip()
    if not name:
        raise services.ServiceError("Name is required")
    if db.execute("SELECT 1 FROM playlists WHERE name = ?", (name,)).fetchone():
        raise services.ServiceError("A playlist with that name already exists")
    cur = db.execute("INSERT INTO playlists (name) VALUES (?)", (name,))
    db.commit()
    return redirect(url_for("ui.playlist", playlist_id=cur.lastrowid))


@bp.get("/playlists/<int:playlist_id>")
def playlist(playlist_id):
    db = get_db()
    localpages.sync_content(db)
    pl = db.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    if pl is None:
        abort(404)
    items = db.execute(
        "SELECT pi.*, c.name, c.url FROM playlist_items pi JOIN content_items c ON c.id = pi.content_id"
        " WHERE pi.playlist_id = ? ORDER BY pi.position, pi.id", (playlist_id,)).fetchall()
    content = db.execute("SELECT * FROM content_items ORDER BY name").fetchall()
    return render_template("playlist.html", playlist=pl, items=items, content=content)


@bp.post("/playlists/<int:playlist_id>/rename")
def playlist_rename(playlist_id):
    db = get_db()
    name = request.form.get("name", "").strip()
    if not name:
        raise services.ServiceError("Name is required")
    if db.execute("SELECT 1 FROM playlists WHERE name = ? AND id != ?", (name, playlist_id)).fetchone():
        raise services.ServiceError("A playlist with that name already exists")
    db.execute("UPDATE playlists SET name = ? WHERE id = ?", (name, playlist_id))
    db.commit()
    return redirect(url_for("ui.playlist", playlist_id=playlist_id))


@bp.post("/playlists/<int:playlist_id>/delete")
def playlist_delete(playlist_id):
    db = get_db()
    db.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
    db.commit()
    return redirect(url_for("ui.playlists"))


@bp.post("/playlists/<int:playlist_id>/items")
def playlist_item_add(playlist_id):
    db = get_db()
    content_id = _int(request.form.get("content_id"))
    if not db.execute("SELECT 1 FROM content_items WHERE id = ?", (content_id,)).fetchone():
        raise services.ServiceError("Choose a content item")
    position = db.execute("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM playlist_items"
                          " WHERE playlist_id = ?", (playlist_id,)).fetchone()["p"]
    db.execute("INSERT INTO playlist_items (playlist_id, content_id, position, duration) VALUES (?, ?, ?, ?)",
               (playlist_id, content_id, position, _int(request.form.get("duration"), 30, 1)))
    db.commit()
    return redirect(url_for("ui.playlist", playlist_id=playlist_id))


@bp.post("/playlists/<int:playlist_id>/items/<int:item_id>/update")
def playlist_item_update(playlist_id, item_id):
    db = get_db()
    db.execute("UPDATE playlist_items SET duration = ? WHERE id = ? AND playlist_id = ?",
               (_int(request.form.get("duration"), 30, 1), item_id, playlist_id))
    db.commit()
    return redirect(url_for("ui.playlist", playlist_id=playlist_id))


@bp.post("/playlists/<int:playlist_id>/items/<int:item_id>/move")
def playlist_item_move(playlist_id, item_id):
    db = get_db()
    ids = [r["id"] for r in db.execute(
        "SELECT id FROM playlist_items WHERE playlist_id = ? ORDER BY position, id", (playlist_id,))]
    if item_id in ids:
        i = ids.index(item_id)
        j = i - 1 if request.form.get("dir") == "up" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
        for position, pid in enumerate(ids, 1):
            db.execute("UPDATE playlist_items SET position = ? WHERE id = ?", (position, pid))
        db.commit()
    return redirect(url_for("ui.playlist", playlist_id=playlist_id))


@bp.post("/playlists/<int:playlist_id>/items/<int:item_id>/delete")
def playlist_item_delete(playlist_id, item_id):
    db = get_db()
    db.execute("DELETE FROM playlist_items WHERE id = ? AND playlist_id = ?", (item_id, playlist_id))
    db.commit()
    return redirect(url_for("ui.playlist", playlist_id=playlist_id))


# --- screen power schedules -------------------------------------------------

def _schedules_page(schedule=None):
    return render_template("schedules.html", schedules=services.list_schedules(get_db()), schedule=schedule)


@bp.get("/schedules")
def schedules():
    return _schedules_page()


@bp.get("/schedules/<int:schedule_id>")
def schedule_edit(schedule_id):
    try:
        return _schedules_page(services.get_schedule(get_db(), schedule_id))
    except services.ServiceError:
        abort(404)


@bp.post("/schedules")
@bp.post("/schedules/<int:schedule_id>")
def schedule_save(schedule_id=None):
    services.save_schedule(get_db(), schedule_id, request.form.get("name"), request.form.get("on"),
                           request.form.get("off"), request.form.getlist("days"))
    return redirect(url_for("ui.schedules"))


@bp.post("/schedules/<int:schedule_id>/delete")
def schedule_delete(schedule_id):
    services.delete_schedule(get_db(), schedule_id)
    return redirect(url_for("ui.schedules"))


# --- local pages ------------------------------------------------------------

@bp.get("/local-pages")
def pages():
    localpages.sync_content(get_db())
    return render_template("pages.html", pages=localpages.list_pages())


@bp.post("/local-pages/upload")
def page_upload():
    f = request.files.get("file")
    if f is None or not f.filename:
        raise services.ServiceError("Choose a file to upload")
    stem = f.filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].rsplit(".", 1)[0]
    label = request.form.get("name", "").strip()
    name = localpages.save_upload(label or stem, f.filename, f.read())
    db = get_db()
    localpages.sync_content(db)
    if label:
        # The folder name is a URL-safe version; content keeps the name as typed.
        db.execute("UPDATE content_items SET name = ? WHERE url = ? AND name = ?",
                   (label, f"/pages/{name}/", name))
        db.commit()
    flash(f"Uploaded page '{label or name}' at /pages/{name}/", "ok")
    return redirect(url_for("ui.pages"))


@bp.post("/local-pages/<name>/delete")
def page_delete(name):
    localpages.delete_upload(name)
    db = get_db()
    # Drop the auto-created content item if nothing customised it.
    db.execute("DELETE FROM content_items WHERE url = ?", (f"/pages/{name}/",))
    db.commit()
    return redirect(url_for("ui.pages"))
