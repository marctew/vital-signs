"""Rotating panes (a playlist cycling inside one box) and Send to screen."""
import html

from test_admin_ui import pane, save_split


def playlist_with(server, name, *content_ids):
    server.post("/playlists", name=name)
    playlist_id = server.ids("playlists")[name]
    for content_id in content_ids:
        server.post(f"/playlists/{playlist_id}/items", content_id=str(content_id), duration="20")
    return playlist_id


def test_every_playlist_gets_a_rotation_that_follows_it(server):
    a, b = server.add_content("A", zoom="1.5"), server.add_content("B", refresh_interval="60")
    playlist_id = playlist_with(server, "Info", a, b)
    server.client.get("/content")
    rotation = server.ids()["Info (rotating)"]
    html = server.client.get(f"/rotate/{rotation}/?display=Left").text
    assert html.count("<iframe") == 2 and 'data-duration="20"' in html
    assert "scale(1.5)" in html and 'data-refresh="60"' in html
    assert html.index("a.example") < html.index("b.example"), "in playlist order"

    server.post(f"/playlists/{playlist_id}/rename", name="Facts")
    server.client.get("/content")
    assert "Facts (rotating)" in server.ids() and "Info (rotating)" not in server.ids()
    server.post(f"/playlists/{playlist_id}/delete")
    server.client.get("/content")
    assert "Facts (rotating)" not in server.ids()
    assert server.client.get(f"/rotate/{rotation}/").status_code == 404


def test_rotation_goes_in_a_split_but_not_in_a_playlist_or_the_bin(server):
    a = server.add_content("A")
    playlist_id = playlist_with(server, "Info", a)
    editor = server.client.get("/content").text
    rotation = server.ids()["Info (rotating)"]
    assert f"var hideable = [{rotation}];" in editor, "a rotating cell can hide when all its items are empty"
    assert "Split screen: Info (rotating)" in save_split(server, "/content/split", "Wall", "2x4", [pane(rotation, 0, 0, 2, 1)]).text
    html = server.client.get(f"/split/{server.ids()['Wall']}/?display=Left").text
    assert f'src="/rotate/{rotation}/?display=Left"' in html, "display details are passed down"

    assert "Choose a content item" in server.post(f"/playlists/{playlist_id}/items", content_id=str(rotation)).text
    assert "(rotating)" not in server.client.get(f"/playlists/{playlist_id}").text.split("<h2>Add item</h2>")[1]
    server.post(f"/content/{rotation}/delete")
    assert "Info (rotating)" in server.ids(), "removed only by deleting its playlist"
    r = server.client.get(f"/content/{rotation}")
    assert r.status_code == 302 and r.headers["Location"] == f"/playlists/{playlist_id}"


def test_split_screens_inside_the_playlist_are_left_out_of_the_rotation(server):
    a = server.add_content("A")
    save_split(server, "/content/split", "Wall", "2x4", [pane(a, 0, 0)])
    playlist_with(server, "Mixed", a, server.ids()["Wall"])
    server.client.get("/content")
    assert server.client.get(f"/rotate/{server.ids()['Mixed (rotating)']}/").text.count("<iframe") == 1


def test_rotation_is_a_local_url_for_the_agent(server):
    a = server.add_content("A")
    playlist_with(server, "Info", a)
    server.client.get("/content")
    rotation = server.ids()["Info (rotating)"]
    server.poll()
    assert server.client.post("/api/v1/displays/1/override", json={"url": f"/rotate/{rotation}/", "minutes": 5}).status_code == 200
    assert server.poll()["displays"]["HDMI-A-1"]["override"]["local"] is True


def test_send_to_screen(server):
    server.poll()
    page = server.client.get("/send?url=https%3A%2F%2Fexample.com%2Frecipe").text
    assert 'value="https://example.com/recipe"' in page and "Send to Left" in page and "Send to all" in page
    bookmark = html.unescape(page.split('class="button" href="')[1].split('"')[0])
    assert bookmark.startswith("javascript:void(window.open('http://localhost/send?url='+encodeURIComponent(location.href)")

    page = server.post("/send", url="https://example.com/recipe", minutes="30", display="1").text
    assert "Showing on Left for 30 minutes." in page and "Stop" in page
    state = server.poll()["displays"]
    assert state["HDMI-A-1"]["override"]["url"] == "https://example.com/recipe" and state["HDMI-A-2"]["override"] is None
    assert 1700 < state["HDMI-A-1"]["override"]["remaining_s"] <= 1800

    server.post("/send", url="https://example.com/map", minutes="5", display="all")
    state = server.poll()["displays"]
    assert {state[c]["override"]["url"] for c in state} == {"https://example.com/map"}
    assert "URL must start with" in server.post("/send", url="javascript:alert(1)", minutes="5", display="1").text


def test_send_finds_the_address_in_shared_text(server):
    server.poll()
    page = server.client.get("/send", query_string={"title": "A recipe", "text": "Look at this https://example.com/r?id=4 nice"}).text
    assert 'value="https://example.com/r?id=4"' in page
    assert 'value=""' in server.client.get("/send", query_string={"text": "no address here"}).text


def test_manifest_offers_a_share_target(server):
    manifest = server.client.get("/manifest.webmanifest").json
    assert manifest["share_target"]["action"] == "/send" and manifest["start_url"] == "/send"
    assert server.client.get("/static/icon.svg").status_code == 200
