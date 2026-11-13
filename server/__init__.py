__version__ = "1.1.1"

from datetime import timedelta

from flask import Flask, abort, request, send_from_directory

from . import agent_api, auth, config, control_api, dataproxy, db, health, localpages, modules, photos, split, ui


def create_app(cfg=None):
    cfg = cfg or config.load()
    data_dir = cfg["data_dir"]
    screenshots = data_dir / "screenshots"
    uploads = data_dir / "pages"
    previews = data_dir / "previews"
    for folder in (data_dir, screenshots, uploads, previews):
        folder.mkdir(parents=True, exist_ok=True)

    app = Flask(__name__)
    app.secret_key = auth.secret_key(data_dir)
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"   # keeps other sites from posting to the admin UI
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
    app.config["VS"] = cfg
    app.config["DB_PATH"] = str(data_dir / "vitalsigns.db")
    app.config["SCREENSHOT_DIR"] = screenshots
    app.config["PREVIEW_DIR"] = previews
    app.config["UPLOAD_PAGES_DIR"] = uploads
    app.config["MAX_CONTENT_LENGTH"] = cfg["max_upload_mb"] * 1024 * 1024

    db.init_app(app)
    for module in (auth, agent_api, control_api, dataproxy, health, localpages, modules, photos, split, ui):
        app.register_blueprint(module.bp)
    try:                                          # the Design system card in Settings; du_flask.py is
        from .du_flask import init_design         # copied in by the design updater, so it may not be here yet
        init_design(app, data_dir / "design", version=__version__)
    except ImportError:
        pass

    @app.after_request
    def cache_headers(resp):
        """APP-STANDARD 12: static files are no-cache, so a deploy shows at once. Every other answer is never
        for a shared cache: no-store if it says nothing (pages, JSON, /healthz, the agent API, redirects,
        errors), and one that set its own caching keeps it, made private (the display pages' no-cache, so a
        screen's browser revalidates rather than refetching on every rotation; screenshots and previews)."""
        if request.endpoint == "static":
            resp.headers["Cache-Control"] = "no-cache"   # revalidate, so a deploy's CSS and JS show at once
            return resp
        words = [w.strip() for w in resp.headers.get("Cache-Control", "").split(",")
                 if w.strip() and w.strip().lower() != "public"]
        if not words:
            words = ["no-store"]
        elif not {"private", "no-store"} & {w.lower() for w in words}:
            words.insert(0, "private")
        resp.headers["Cache-Control"] = ", ".join(words)
        return resp

    @app.get("/screenshots/<int:display_id>.jpg")
    def screenshot(display_id):
        path = screenshots / f"{display_id}.jpg"
        if not path.is_file():
            abort(404)
        return send_from_directory(screenshots, path.name, max_age=0)

    @app.get("/previews/<int:content_id>.jpg")
    def preview(content_id):
        if not (previews / f"{content_id}.jpg").is_file():
            abort(404)
        return send_from_directory(previews, f"{content_id}.jpg", max_age=0)

    return app
