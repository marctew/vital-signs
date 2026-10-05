"""Photo albums for the Photo frame module.

An album is a folder of pictures under <data_dir>/photos. Uploads are turned
the right way up, scaled down to what a screen can show and saved as JPEG, so
a wall of phone photos does not overwhelm the Pi.
"""
import io
import re
import shutil
from urllib.parse import quote

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_from_directory, url_for
from PIL import Image, ImageOps, UnidentifiedImageError

from .services import ServiceError

bp = Blueprint("photos", __name__)

ALBUM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,49}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.()-]{0,99}\.jpg$")
MAX_SIDE = 2560
MAX_PHOTOS = 2000


def root():
    folder = current_app.config["VS"]["data_dir"] / "photos"
    folder.mkdir(exist_ok=True)
    return folder


def album_dir(name):
    """The folder for an album, or None when the name is not one we would create."""
    return root() / name if ALBUM_RE.match(name or "") else None


def list_albums():
    albums = []
    for folder in sorted(root().iterdir(), key=lambda p: p.name.lower()):
        if folder.is_dir() and ALBUM_RE.match(folder.name):
            albums.append({"name": folder.name, "photos": list_photos(folder.name)})
    return albums


def list_photos(album):
    folder = album_dir(album)
    if folder is None or not folder.is_dir():
        return []
    return [{"name": p.name, "url": f"/photos/{quote(album)}/{quote(p.name)}"}
            for p in sorted(folder.iterdir(), key=lambda p: p.name.lower()) if FILE_RE.match(p.name)]


def save_photo(album, filename, data):
    """Store one uploaded picture in an album. Raises ServiceError if it is not a usable image."""
    folder = album_dir(album)
    if folder is None or not folder.is_dir():
        raise ServiceError("No such album", 404)
    if len(list_photos(album)) >= MAX_PHOTOS:
        raise ServiceError(f"An album holds at most {MAX_PHOTOS} photos")
    try:
        image = Image.open(io.BytesIO(data))
        image = ImageOps.exif_transpose(image)      # phones store rotation separately
        image.thumbnail((MAX_SIDE, MAX_SIDE))
        image = image.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise ServiceError(f"{filename} is not a picture this server can read (JPEG, PNG and WebP work)")
    stem = re.sub(r"[^A-Za-z0-9 _()-]+", "-", filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].rsplit(".", 1)[0]).strip("-. ") or "photo"
    name, n = f"{stem[:80]}.jpg", 1
    while (folder / name).exists():
        n += 1
        name = f"{stem[:80]} ({n}).jpg"
    image.save(folder / name, "JPEG", quality=86)
    return name


# --- what the displays load ---------------------------------------------------

@bp.get("/photos/<album>/<filename>")
def serve(album, filename):
    folder = album_dir(album)
    if folder is None or not FILE_RE.match(filename):
        abort(404)
    return send_from_directory(folder, filename, max_age=3600)


# --- admin pages ---------------------------------------------------------------

@bp.errorhandler(ServiceError)
def service_error(e):
    flash(str(e), "error")
    return redirect(request.referrer or url_for("photos.albums"))


@bp.get("/albums")
def albums():
    return render_template("albums.html", albums=list_albums())


@bp.post("/albums")
def album_create():
    name = " ".join(request.form.get("name", "").split())
    folder = album_dir(name)
    if folder is None:
        raise ServiceError("Album names use letters, digits, spaces, - and _")
    if folder.exists():
        raise ServiceError("An album with that name already exists")
    folder.mkdir()
    return redirect(url_for("photos.albums"))


@bp.post("/albums/<album>/upload")
def album_upload(album):
    files = [f for f in request.files.getlist("files") if f.filename]
    if not files:
        raise ServiceError("Choose some photos to upload")
    added, problems = 0, []
    for f in files:
        try:
            save_photo(album, f.filename, f.read())
            added += 1
        except ServiceError as e:
            if e.status == 404:
                raise
            problems.append(str(e))
    if added:
        flash(f"Added {added} photo{'s' if added != 1 else ''} to {album}", "ok")
    for problem in problems[:5]:
        flash(problem, "error")
    return redirect(url_for("photos.albums"))


@bp.post("/albums/<album>/photos/<filename>/delete")
def photo_delete(album, filename):
    folder = album_dir(album)
    if folder is None or not FILE_RE.match(filename):
        abort(404)
    (folder / filename).unlink(missing_ok=True)
    return redirect(url_for("photos.albums"))


@bp.post("/albums/<album>/delete")
def album_delete(album):
    folder = album_dir(album)
    if folder is None or not folder.is_dir():
        abort(404)
    shutil.rmtree(folder)
    return redirect(url_for("photos.albums"))
