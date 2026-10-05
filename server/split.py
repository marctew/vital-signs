"""Split screens: one content item that shows two or three others in panes.

The panes are iframes on a page served at /split/<id>/, so a split is an
ordinary URL to the agent and works in playlists, overrides and schedules.
Sites that refuse to be framed are handled on the agent, which strips the
blocking response headers for split tabs.
"""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import Blueprint, abort, render_template, request

from . import services
from .db import get_db

bp = Blueprint("split", __name__)

DISPLAY_PARAMS = ("display", "orientation")


def _pass_display_details(entries):
    """Local pages inside a split or rotation learn which display they are on, like any local page."""
    passed = [(k, request.args[k]) for k in DISPLAY_PARAMS if k in request.args]
    for entry in entries:
        if services.is_local_url(entry["url"]) and passed:
            parts = urlsplit(entry["url"])
            query = parse_qsl(parts.query, keep_blank_values=True) + passed
            entry["url"] = urlunsplit(parts._replace(query=urlencode(query)))


@bp.get("/split/<int:content_id>/")
def view(content_id):
    split = services.get_split(get_db(), content_id)
    if split is None:
        abort(404)
    _pass_display_details(split["panes"])
    resp = render_template("split.html", split=split)
    return resp, 200, {"Cache-Control": "no-cache"}


@bp.get("/rotate/<int:content_id>/")
def rotate(content_id):
    rotation = services.get_rotation(get_db(), content_id)
    if rotation is None:
        abort(404)
    _pass_display_details(rotation["items"])
    return render_template("rotate.html", rotation=rotation), 200, {"Cache-Control": "no-cache"}
