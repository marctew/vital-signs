"""Static files and caching (APP-STANDARD 12): pages are no-store, static files no-cache, and every static link
carries ?v=<mtime>, so a deploy's CSS and JS show at once, through the tunnel as well as on the LAN. The display
pages keep their own no-cache."""
import re

from server import __version__


def _static_links(html):
    return re.findall(r'(?:href|src)="(/static/[^"]*)"', html)


def test_static_links_carry_a_version(server):
    html = server.client.get("/").get_data(as_text=True)
    links = _static_links(html)
    assert links, "the page links no static files"
    for link in links:
        # the home screen icon and manifest keep their own v=app_version (PWA-ICONS.md)
        assert re.search(r"\?v=\d+$", link) or link.endswith(f"?v={__version__}"), link
    assert any(link.startswith("/static/vitalsigns.css?v=") for link in links)


def test_pages_are_no_store(server):
    r = server.client.get("/")
    assert r.status_code == 200 and r.mimetype == "text/html"
    assert r.headers["Cache-Control"] == "no-store"


def test_static_files_are_no_cache(server):
    r = server.client.get("/static/vitalsigns.css")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-cache"


def test_display_pages_keep_their_own_caching(server):
    from tests.test_admin_ui import pane, save_split
    r = server.client.get("/pages/clock/")
    assert r.status_code == 200 and r.mimetype == "text/html"
    assert r.headers["Cache-Control"] == "no-cache"
    a = server.add_content("A")
    save_split(server, "/content/split", "Wall", "2x4", [pane(a, 0, 0)])
    r = server.client.get(f"/split/{server.ids()['Wall']}/")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-cache"
