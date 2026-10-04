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


@bp.get("/split/<int:content_id>/")
def view(content_id):
    split = services.get_split(get_db(), content_id)
    if split is None:
        abort(404)
    # Local pages in a pane learn which display they are on, like any local page.
    passed = [(k, request.args[k]) for k in DISPLAY_PARAMS if k in request.args]
    for pane in split["panes"]:
        if services.is_local_url(pane["url"]) and passed:
            parts = urlsplit(pane["url"])
            query = parse_qsl(parts.query, keep_blank_values=True) + passed
            pane["url"] = urlunsplit(parts._replace(query=urlencode(query)))
    resp = render_template("split.html", split=split)
    return resp, 200, {"Cache-Control": "no-cache"}
