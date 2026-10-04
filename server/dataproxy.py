"""Data proxy: pages fetch /api/data/<source>; the server calls the configured URL.

Only sources named in the server config can be fetched, so this is not an
open proxy, and secrets in the URL or headers never reach page source.
"""
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import Blueprint, Response, current_app, jsonify, request

bp = Blueprint("dataproxy", __name__)

MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = 10

_cache = {}
_lock = threading.Lock()


@bp.get("/api/data/<name>")
def fetch(name):
    source = current_app.config["VS"]["data_sources"].get(name)
    if not source or not str(source.get("url", "")).startswith(("http://", "https://")):
        return jsonify(error="unknown data source"), 404

    allowed = source.get("allow_params") or []
    extra = sorted((k, v) for k, v in request.args.items() if k in allowed)
    parts = urlsplit(source["url"])
    query = urlencode(parse_qsl(parts.query, keep_blank_values=True) + extra)
    url = urlunsplit(parts._replace(query=query))

    ttl = float(source.get("cache_seconds", 60))
    key = (name, tuple(extra))
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return Response(hit[1], mimetype=hit[2])

    req = urllib.request.Request(url, headers={"User-Agent": "vitalsigns", **source.get("headers", {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read(MAX_BYTES + 1)
            mimetype = r.headers.get_content_type()
    except (urllib.error.URLError, OSError) as e:
        if hit:  # serve stale data rather than nothing
            return Response(hit[1], mimetype=hit[2])
        reason = e.code if isinstance(e, urllib.error.HTTPError) else "unreachable"
        return jsonify(error=f"upstream error: {reason}"), 502
    if len(body) > MAX_BYTES:
        return jsonify(error="upstream response too large"), 502
    with _lock:
        _cache[key] = (time.time(), body, mimetype)
    return Response(body, mimetype=mimetype)
