import os

from flask import Flask, abort, send_from_directory

from . import agent_api, config, control_api, dataproxy, db, localpages, ui


def create_app(cfg=None):
    cfg = cfg or config.load()
    data_dir = cfg["data_dir"]
    screenshots = data_dir / "screenshots"
    uploads = data_dir / "pages"
    for folder in (data_dir, screenshots, uploads):
        folder.mkdir(parents=True, exist_ok=True)

    app = Flask(__name__)
    app.secret_key = os.urandom(32)  # only signs flash messages
    app.config["VS"] = cfg
    app.config["DB_PATH"] = str(data_dir / "vitalsigns.db")
    app.config["SCREENSHOT_DIR"] = screenshots
    app.config["UPLOAD_PAGES_DIR"] = uploads
    app.config["MAX_CONTENT_LENGTH"] = cfg["max_upload_mb"] * 1024 * 1024

    db.init_app(app)
    for module in (agent_api, control_api, dataproxy, localpages, ui):
        app.register_blueprint(module.bp)

    @app.get("/screenshots/<int:display_id>.jpg")
    def screenshot(display_id):
        path = screenshots / f"{display_id}.jpg"
        if not path.is_file():
            abort(404)
        return send_from_directory(screenshots, path.name, max_age=0)

    return app
