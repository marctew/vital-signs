"""Local pages: one folder per page with an index.html, served at /pages/<name>/.

Pages come from pages/ in the repo and from <data_dir>/pages (uploads).
"""
import io
import re
import shutil
import stat
import zipfile

from flask import Blueprint, abort, current_app, redirect, send_from_directory

from shared.protocol import REPO_ROOT

from .services import ServiceError

bp = Blueprint("localpages", __name__)

REPO_PAGES = REPO_ROOT / "pages"
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
MAX_FILES = 2000


def upload_dir():
    return current_app.config["UPLOAD_PAGES_DIR"]


def _scan(folder, source):
    if not folder.is_dir():
        return []
    return [{"name": p.name, "source": source}
            for p in sorted(folder.iterdir())
            if p.is_dir() and NAME_RE.match(p.name) and not p.name.startswith("_")
            and (p / "index.html").is_file()]


def list_pages():
    pages = _scan(REPO_PAGES, "repo")
    seen = {p["name"] for p in pages}
    pages += [p for p in _scan(upload_dir(), "upload") if p["name"] not in seen]
    return pages


def _find(name):
    if not NAME_RE.match(name):
        return None
    for root in (REPO_PAGES, upload_dir()):
        if (root / name).is_dir():
            return root / name
    return None


@bp.get("/pages/<name>")
def page_noslash(name):
    return redirect(f"/pages/{name}/")


@bp.get("/pages/<name>/", defaults={"filename": "index.html"})
@bp.get("/pages/<name>/<path:filename>")
def serve(name, filename):
    folder = _find(name)
    if folder is None:
        abort(404)
    resp = send_from_directory(folder, filename)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def clean_name(raw):
    name = re.sub(r"[^a-z0-9_-]+", "-", (raw or "").lower()).strip("-_")
    if not name or not NAME_RE.match(name):
        raise ServiceError("Page name must use letters, digits, - or _")
    return name


def _safe_members(zf, max_bytes):
    """Validated (relative path, ZipInfo) pairs. Rejects anything that could escape."""
    members = []
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ServiceError("Zip contains a symlink")
        path = info.filename.replace("\\", "/")
        parts = path.split("/")
        if path.startswith("/") or ":" in parts[0] or any(p in ("", ".", "..") for p in parts):
            raise ServiceError(f"Unsafe path in zip: {info.filename}")
        if parts[0] == "__MACOSX":
            continue
        total += info.file_size
        if total > max_bytes:
            raise ServiceError("Zip is too large when unpacked")
        members.append((parts, info))
    if len(members) > MAX_FILES:
        raise ServiceError("Zip contains too many files")
    # A zip of a single folder: use that folder as the page root.
    if members and all(len(p) > 1 and p[0] == members[0][0][0] for p, _ in members):
        members = [(p[1:], i) for p, i in members]
    if not any(p == ["index.html"] for p, _ in members):
        raise ServiceError("Zip must contain an index.html at its top level")
    return members


def save_upload(name, filename, data):
    """Store an uploaded .zip or .html as the page `name`, replacing an earlier upload."""
    name = clean_name(name)
    if (REPO_PAGES / name).exists():
        raise ServiceError(f"'{name}' is a repo page and cannot be replaced by an upload")
    max_bytes = current_app.config["VS"]["max_upload_mb"] * 1024 * 1024
    root = upload_dir()
    staging = root / f".staging-{name}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        lower = filename.lower()
        if lower.endswith(".zip"):
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                raise ServiceError("Not a valid zip file")
            base = staging.resolve()
            written = 0
            for parts, info in _safe_members(zf, max_bytes):
                dest = (staging.joinpath(*parts)).resolve()
                if not dest.is_relative_to(base):
                    raise ServiceError(f"Unsafe path in zip: {info.filename}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Count real bytes: file_size in the header can lie.
                with zf.open(info) as src, open(dest, "wb") as out:
                    while chunk := src.read(65536):
                        written += len(chunk)
                        if written > max_bytes:
                            raise ServiceError("Zip is too large when unpacked")
                        out.write(chunk)
        elif lower.endswith((".html", ".htm")):
            (staging / "index.html").write_bytes(data)
        else:
            raise ServiceError("Upload a .zip or a single .html file")
        final = root / name
        shutil.rmtree(final, ignore_errors=True)
        staging.replace(final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return name


def delete_upload(name):
    if not NAME_RE.match(name) or not (upload_dir() / name).is_dir():
        raise ServiceError("No such uploaded page", 404)
    shutil.rmtree(upload_dir() / name)


def sync_content(db):
    """Offer every local page as content: create an item for any page that has none."""
    for page in list_pages():
        url = f"/pages/{page['name']}/"
        if not db.execute("SELECT 1 FROM content_items WHERE url = ? OR url LIKE ?",
                          (url, url + "?%")).fetchone():
            db.execute("INSERT INTO content_items (name, url) VALUES (?, ?)", (page["name"], url))
    db.commit()
