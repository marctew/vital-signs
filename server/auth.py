"""Optional login for the admin UI.

Auth is off until a password is set with `python -m server set-password`,
which stores a hash in the data folder. Pages shown on the displays
(/pages/, /api/data/) and the agent API stay reachable without a login:
the kiosk browsers and agents have no session.
"""
import hmac
import os
import time

from flask import (Blueprint, current_app, jsonify, redirect, render_template, request, session,
                   url_for)
from werkzeug.security import check_password_hash, generate_password_hash

bp = Blueprint("auth", __name__)

OPEN_PREFIXES = ("/static/", "/pages/", "/split/", "/rotate/", "/photos/", "/api/agent/", "/api/data/", "/api/module/", "/login")


def _password_file(data_dir=None):
    return (data_dir or current_app.config["VS"]["data_dir"]) / "admin-password.hash"


def enabled():
    return _password_file().is_file()


def set_password(data_dir, password):
    path = _password_file(data_dir)
    path.write_text(generate_password_hash(password), encoding="utf-8")
    if os.name == "posix":
        os.chmod(path, 0o600)
        owner = data_dir.stat()
        os.chown(path, owner.st_uid, owner.st_gid)     # the service may run as another user


def clear_password(data_dir):
    _password_file(data_dir).unlink(missing_ok=True)


def secret_key(data_dir):
    """Session signing key, kept across restarts so logins survive them."""
    path = data_dir / "secret-key"
    if not path.is_file():
        path.write_bytes(os.urandom(32))
        if os.name == "posix":
            os.chmod(path, 0o600)
    return path.read_bytes()


def has_valid_token():
    token = current_app.config["VS"]["control_token"]
    sent = request.headers.get("Authorization", "")
    return bool(token) and hmac.compare_digest(sent.encode(), f"Bearer {token}".encode())


@bp.before_app_request
def require_login():
    if request.path.startswith(OPEN_PREFIXES):
        return None
    if request.path.startswith("/api/v1/"):
        # The control API takes a logged-in session or the control token. It is
        # only open when neither a password nor a token has been configured.
        if session.get("admin") or has_valid_token():
            return None
        if not enabled() and not current_app.config["VS"]["control_token"]:
            return None
        return jsonify(error="unauthorized"), 401
    if not enabled() or session.get("admin"):
        return None
    return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))


def _safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") else url_for("ui.dashboard")


@bp.route("/login", methods=["GET", "POST"])
def login():
    target = _safe_next(request.values.get("next"))
    if not enabled():
        return redirect(target)
    error = None
    if request.method == "POST":
        if check_password_hash(_password_file().read_text(encoding="utf-8").strip(),
                               request.form.get("password", "")):
            session.clear()
            session["admin"] = True
            session.permanent = True
            return redirect(target)
        time.sleep(1)   # slow down guessing
        error = "Wrong password"
    return render_template("login.html", error=error, next=target)


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.app_context_processor
def template_flags():
    return {"auth_enabled": enabled()}
