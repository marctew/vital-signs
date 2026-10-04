"""Admin pages: dashboard, a display's page, content, playlists, uploads, split screens."""
import io
import zipfile


def make_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return buffer.getvalue()


def upload(server, name, filename, data):
    return server.client.post("/local-pages/upload", data={"name": name, "file": (io.BytesIO(data), filename)},
                              content_type="multipart/form-data", follow_redirects=True)


def test_every_page_renders(server):
    server.poll()
    server.add_content("News")
    server.post("/playlists", name="Main")
    for path in ("/", "/displays/1", "/content", "/content/1", "/playlists", "/playlists/1", "/schedules", "/local-pages"):
        assert server.client.get(path).status_code == 200, path
    assert server.client.get("/displays/99").status_code == 404


def test_dashboard_is_an_overview_and_display_page_has_the_settings(server):
    server.poll()
    dashboard = server.client.get("/").text
    assert 'href="/displays/1"' in dashboard and "Identify" in dashboard
    assert "Playlists by time" not in dashboard and "Copy all schedules from" not in dashboard
    page = server.client.get("/displays/1").text
    for part in ("Playlists by time", "Screen power", "Zoom for everything on this display", "Identify"):
        assert part in page, part


def test_display_actions_return_to_the_display_page(server):
    server.poll()
    r = server.client.post("/displays/1/zoom", data={"zoom": "1.5"})
    assert r.status_code == 302 and r.headers["Location"] == "/displays/1"
    r = server.client.post("/displays/1/command/identify", headers={"Referer": "http://localhost/"})
    assert r.headers["Location"] == "http://localhost/", "pressed on the dashboard: back to the dashboard"
    assert server.client.get("/api/v1/displays/1").json["zoom"] == 1.5


def test_content_urls_are_validated(server):
    assert "URL must start with" in server.post("/content", name="x", url="file:///etc/passwd").text
    assert "Name is required" in server.post("/content", name="", url="https://a.example").text
    assert server.add_content("Ok", "/pages/clock/?tz=UTC")


def test_playlist_items_can_be_reordered_and_removed(server):
    a, b = server.add_content("A"), server.add_content("B")
    server.post("/playlists", name="Main")
    server.post("/playlists/1/items", content_id=str(a), duration="10")
    server.post("/playlists/1/items", content_id=str(b), duration="20")
    order = lambda: [r[0] for r in server.db().execute(
        "SELECT c.name FROM playlist_items pi JOIN content_items c ON c.id = pi.content_id ORDER BY pi.position")]
    assert order() == ["A", "B"]
    server.post("/playlists/1/items/2/move", dir="up")
    assert order() == ["B", "A"]
    server.post(f"/content/{a}/delete")
    assert order() == ["B"], "deleting content removes it from playlists"


def test_uploads_keep_the_typed_name_and_refuse_unsafe_zips(server):
    page = upload(server, "Sales Board", "report.html", b"<p>hi</p>").text
    assert "Sales Board" in page and server.client.get("/pages/sales-board/").data == b"<p>hi</p>"
    assert server.ids()["Sales Board"]

    upload(server, "demo", "demo.zip", make_zip({"demo/index.html": "<h1>x</h1>", "demo/a/b.css": "x"}))
    assert server.client.get("/pages/demo/a/b.css").status_code == 200, "a single top folder is unwrapped"

    assert "Unsafe path" in upload(server, "evil", "evil.zip", make_zip({"index.html": "x", "../../evil.txt": "x"})).text
    assert server.client.get("/pages/evil/").status_code == 404
    assert not (server.data_dir.parent / "evil.txt").exists()
    assert "must contain an index.html" in upload(server, "empty", "e.zip", make_zip({"readme.txt": "x"})).text
    assert "repo page" in upload(server, "clock", "c.html", b"x").text
    assert server.client.get("/pages/clock/../../server/config.py").status_code == 404


def save_split(server, path, name, grid, panes):
    form = {"name": name, "grid": grid}
    for key in ("content_id", "col", "row", "col_span", "row_span"):
        form[key] = [str(p[key]) for p in panes]
    return server.post(path, **form)


def pane(content_id, col, row, col_span=1, row_span=1):
    return {"content_id": content_id, "col": col, "row": row, "col_span": col_span, "row_span": row_span}


def test_split_screen_grid(server):
    a, b, c = (server.add_content(n) for n in "ABC")
    page = save_split(server, "/content/split", "Wall", "2x4", [pane(a, 0, 0, 2, 2), pane(b, 0, 2, 1, 2), pane(c, 1, 2)]).text
    assert "Split screen: A / B / C" in page
    split_id = server.ids()["Wall"]
    html = server.client.get(f"/split/{split_id}/?display=Left&orientation=portrait").text
    assert "repeat(2, 1fr)" in html and "repeat(4, 1fr)" in html and html.count("<iframe") == 3
    assert "grid-column: 1 / span 2; grid-row: 1 / span 2" in html

    assert "Two panes overlap" in save_split(server, "/content/split", "x", "2x4", [pane(a, 0, 0, 2, 2), pane(b, 1, 1)]).text
    assert "outside the grid" in save_split(server, "/content/split", "x", "2x4", [pane(a, 1, 0, 2, 1)]).text
    assert "Place at least one page" in save_split(server, "/content/split", "x", "2x4", []).text
    assert "cannot contain another split" in save_split(server, "/content/split", "x", "2x4", [pane(split_id, 0, 0)]).text

    save_split(server, f"/content/{split_id}/split", "Wall", "4x2", [pane(a, 0, 0, 4, 1)])
    html = server.client.get(f"/split/{split_id}/").text
    assert "repeat(4, 1fr)" in html and html.count("<iframe") == 1
    assert server.client.get("/split/999/").status_code == 404


def test_split_passes_display_details_to_local_pages(server):
    server.client.get("/content")
    clock = server.add_content("Clock page", "/pages/clock/")
    save_split(server, "/content/split", "Wall", "2x4", [pane(clock, 0, 0)])
    html = server.client.get(f"/split/{server.ids()['Wall']}/?display=Left&orientation=portrait").text
    assert 'src="/pages/clock/?display=Left&amp;orientation=portrait"' in html
