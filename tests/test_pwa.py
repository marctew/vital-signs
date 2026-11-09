"""Home screen icons and the manifest (Design Uniformity PWA-ICONS.md)."""
import io
import json
import re

from PIL import Image

from server import auth


def signed_out(server):
    """A client with no session, after a password is set, as a phone adding the app sees it."""
    auth.set_password(server.data_dir, "correct horse")
    return server.app.test_client()


def test_icons_load_without_signing_in(server):
    anon = signed_out(server)
    for name, size in (("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)):
        r = anon.get(f"/static/{name}")
        assert r.status_code == 200 and r.mimetype == "image/png", name
        img = Image.open(io.BytesIO(r.data))
        assert img.size == (size, size) and img.mode == "RGB"              # opaque: no black corners on iOS
    ico = anon.get("/static/favicon.ico")
    assert ico.status_code == 200 and Image.open(io.BytesIO(ico.data)).size == (32, 32)
    assert anon.get("/static/img/icon.svg").status_code == 200


def test_manifest(server):
    anon = signed_out(server)
    r = anon.get("/static/manifest.json")
    assert r.status_code == 200 and r.mimetype in ("application/json", "application/manifest+json")
    m = json.loads(r.data)
    assert m["name"] == m["short_name"] == "Vital Signs" and m["display"] == "standalone"
    assert m["background_color"] == m["theme_color"] == "#090b11"
    assert {i["src"] for i in m["icons"]} == {"/static/icon-192.png", "/static/icon-512.png"}
    assert any(i.get("purpose") == "maskable" for i in m["icons"])
    assert m["share_target"]["action"] == "/send"                          # Send to screen in Android's share menu


def test_old_addresses_redirect_without_signing_in(server):
    anon = signed_out(server)
    for old, new in (("/manifest.webmanifest", "/static/manifest.json"), ("/favicon.ico", "/static/favicon.ico"),
                     ("/static/icon.svg", "/static/img/icon.svg")):
        r = anon.get(old)
        location = r.headers["Location"].partition("?v=")[0]              # static links carry ?v= (APP-STANDARD 12)
        assert r.status_code == 301 and location.endswith(new), old


def test_head_tags_on_the_app_and_the_login_page(server):
    pages = [server.client.get("/").text]
    anon = signed_out(server)
    login = anon.get("/login")
    assert login.status_code == 200
    pages.append(login.text)
    for html in pages:
        assert 'rel="apple-touch-icon" href="/static/apple-touch-icon.png?v=' in html
        assert '<meta name="apple-mobile-web-app-title" content="Vital Signs">' in html
        assert '<meta name="apple-mobile-web-app-capable" content="yes">' in html
        assert '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">' in html
        assert 'rel="manifest" href="/static/manifest.json?v=' in html
        assert re.search(r'href="/static/img/icon.svg\?v=\d+"', html)
        assert re.search(r'href="/static/favicon.ico\?v=\d+" sizes="32x32"', html)
        assert html.count('<meta name="theme-color"') == 1                  # only the one from theme_head.html
