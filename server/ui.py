"""Server-rendered admin UI. Mutations go through services, same as the control API."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from . import localpages, services
from .db import get_db

bp = Blueprint("ui", __name__)


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
    content = db.execute("SELECT name, url FROM content_items ORDER BY name").fetchall()
    return render_template("dashboard.html", displays=services.list_displays(db),
                           day_names=("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
                           playlists=services.list_playlists(db), content=content)


@bp.post("/displays/<int:display_id>/assign")
def assign(display_id):
    db = get_db()
    playlist_id = request.form.get("playlist_id")
    services.assign(db, services.get_display(db, display_id)["id"],
                    int(playlist_id) if playlist_id else None)
    return redirect(url_for("ui.dashboard"))


@bp.post("/displays/<int:display_id>/override")
def push_override(display_id):
    db = get_db()
    d = services.get_display(db, display_id)
    services.push_override(db, d["id"], request.form.get("url"), request.form.get("minutes"))
    return redirect(url_for("ui.dashboard"))


@bp.post("/displays/<int:display_id>/override/clear")
def clear_override(display_id):
    db = get_db()
    services.clear_override(db, services.get_display(db, display_id)["id"])
    return redirect(url_for("ui.dashboard"))


@bp.post("/displays/<int:display_id>/command/<type_>")
def command(display_id, type_):
    db = get_db()
    services.queue_command(db, services.get_display(db, display_id), type_)
    flash(f"Sent {type_}", "ok")
    return redirect(url_for("ui.dashboard"))


@bp.post("/displays/<int:display_id>/zoom")
def display_zoom(display_id):
    db = get_db()
    services.set_display_zoom(db, services.get_display(db, display_id)["id"], request.form.get("zoom"))
    return redirect(url_for("ui.dashboard"))


@bp.post("/displays/<int:display_id>/power")
def power_schedule(display_id):
    db = get_db()
    d = services.get_display(db, display_id)
    schedule = None
    if request.form.get("enabled"):
        schedule = {"on": request.form.get("on"), "off": request.form.get("off"),
                    "days": request.form.getlist("days")}
    services.set_power_schedule(db, d["id"], schedule)
    flash(f"Screen schedule saved for {d['name']}", "ok")
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


@bp.get("/content")
def content():
    db = get_db()
    localpages.sync_content(db)
    items = db.execute("SELECT * FROM content_items ORDER BY name").fetchall()
    return render_template("content.html", items=items, item=None, pages=localpages.list_pages())


@bp.post("/content")
def content_create():
    db = get_db()
    db.execute("INSERT INTO content_items (name, url, zoom, refresh_interval, css) VALUES (?, ?, ?, ?, ?)",
               _content_form())
    db.commit()
    return redirect(url_for("ui.content"))


@bp.get("/content/<int:item_id>")
def content_edit(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM content_items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    items = db.execute("SELECT * FROM content_items ORDER BY name").fetchall()
    return render_template("content.html", items=items, item=item, pages=localpages.list_pages())


@bp.post("/content/<int:item_id>")
def content_update(item_id):
    db = get_db()
    db.execute("UPDATE content_items SET name = ?, url = ?, zoom = ?, refresh_interval = ?, css = ?"
               " WHERE id = ?", (*_content_form(), item_id))
    db.commit()
    return redirect(url_for("ui.content"))


@bp.post("/content/<int:item_id>/delete")
def content_delete(item_id):
    db = get_db()
    db.execute("DELETE FROM content_items WHERE id = ?", (item_id,))
    db.commit()
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
