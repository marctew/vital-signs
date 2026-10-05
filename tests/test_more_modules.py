"""Photo albums, the Board module's push API, QR codes, and the sun and air providers."""
import io
import json

import pytest
from PIL import Image

from server import modules


def picture(size=(4000, 3000), fmt="JPEG", exif_orientation=None):
    image = Image.new("RGB", size, "#3b5bdb")
    buffer = io.BytesIO()
    if exif_orientation:
        exif = image.getexif()
        exif[0x0112] = exif_orientation
        image.save(buffer, fmt, exif=exif)
    else:
        image.save(buffer, fmt)
    return buffer.getvalue()


def upload(server, album, *files):
    return server.client.post(f"/albums/{album}/upload", data={"files": [(io.BytesIO(data), name) for name, data in files]},
                              content_type="multipart/form-data", follow_redirects=True)


def test_photo_albums(server):
    assert "letters, digits" in server.post("/albums", name="../etc").text
    server.post("/albums", name="Holidays")
    assert "already exists" in server.post("/albums", name="Holidays").text
    page = upload(server, "Holidays", ("IMG 1.jpg", picture()), ("shot.png", picture((800, 600), "PNG")),
                  ("notes.txt", b"not a picture")).text
    assert "Added 2 photos to Holidays" in page and "notes.txt is not a picture" in page
    folder = server.data_dir / "photos" / "Holidays"
    assert sorted(p.name for p in folder.iterdir()) == ["IMG 1.jpg", "shot.jpg"]
    with Image.open(folder / "IMG 1.jpg") as saved:
        assert saved.size == (2560, 1920), "scaled down to what a screen can show"
    upload(server, "Holidays", ("IMG 1.jpg", picture((100, 100))))
    assert (folder / "IMG 1 (2).jpg").is_file(), "a second photo with the same name is kept, not overwritten"
    assert upload(server, "Nope", ("a.jpg", picture((10, 10)))).status_code == 200
    assert not (server.data_dir / "photos" / "Nope").exists()

    assert server.client.get("/photos/Holidays/shot.jpg").status_code == 200
    for bad in ("/photos/Holidays/..%2f..%2fvitalsigns.db", "/photos/Holidays/secret.txt", "/photos/..%2f/x.jpg"):
        assert server.client.get(bad).status_code in (404, 308), bad      # never served
    server.post("/albums/Holidays/photos/shot.jpg/delete")
    assert not (folder / "shot.jpg").exists()


def test_phone_photos_are_turned_the_right_way_up(server):
    server.post("/albums", name="Phone")
    upload(server, "Phone", ("portrait.jpg", picture((400, 300), exif_orientation=6)))
    with Image.open(server.data_dir / "photos" / "Phone" / "portrait.jpg") as saved:
        assert saved.size == (300, 400)


def test_photo_frame_lists_its_album(server):
    server.post("/albums", name="Holidays")
    upload(server, "Holidays", ("b.jpg", picture((50, 50))), ("a.jpg", picture((50, 50))))
    server.post("/content/module/photos", name="Frame", opt_album="Holidays", opt_seconds="10", opt_order="name", opt_fit="cover")
    data = server.client.get(f"/api/module/{server.ids()['Frame']}/data").json
    assert [p["url"] for p in data["photos"]] == ["/photos/Holidays/a.jpg", "/photos/Holidays/b.jpg"]
    server.post("/content/module/photos", name="Lost", opt_album="Missing")
    r = server.client.get(f"/api/module/{server.ids()['Lost']}/data")
    assert r.status_code == 400 and "no album called Missing" in r.json["error"]


def test_board_shows_what_was_pushed(server, monkeypatch):
    server.post("/content/module/board", name="Workshop", opt_stale_minutes="30", opt_empty="Waiting")
    board = server.ids()["Workshop"]
    data = lambda: server.client.get(f"/api/module/{board}/data").json
    assert data() == {"payload": None, "updated": None}
    r = server.client.post("/api/v1/boards/Workshop", json={
        "value": 12.0, "label": "Open jobs", "status": "warn", "junk": {"nested": 1},
        "items": [{"label": "Smith", "value": "09:30", "status": "ok"}, "Parts due", {"label": ""}, 7]})
    assert r.status_code == 200
    assert data()["payload"] == {"value": "12", "label": "Open jobs", "status": "warn", "items": [
        {"label": "Smith", "value": "09:30", "status": "ok"}, {"label": "Parts due"}, {"label": "7"}]}
    assert server.client.post(f"/api/v1/boards/{board}", json={"text": "All done"}).status_code == 200
    assert data()["payload"] == {"text": "All done"}, "each push replaces the last, and shows straight away"
    assert server.client.post("/api/v1/boards/Nope", json={"value": 1}).status_code == 404
    assert server.client.post("/api/v1/boards/Workshop", json=[1, 2]).status_code == 400
    assert server.client.post("/api/v1/boards/Workshop", json={"items": "x"}).status_code == 400

    monkeypatch.setattr(modules, "_cache", {})
    real = modules.time.time()
    monkeypatch.setattr(modules.time, "time", lambda: real + 31 * 60)
    assert data()["payload"] is None, "nothing pushed for longer than the stale setting"


def test_board_push_needs_the_token_when_one_is_set(make_server):
    server = make_server(control_token="ctl")
    assert server.client.post("/api/v1/boards/x", json={"value": 1}).status_code == 401


def test_qr_codes(server):
    server.post("/content/module/qr", name="Link", opt_kind="text", opt_content="https://example.com/menu")
    link = server.client.get(f"/api/module/{server.ids()['Link']}/data").json["svg"]
    assert link.startswith("<svg") and "#fff" in link
    assert 'xmlns="http://www.w3.org/2000/svg"' in link, "needed for the page to show it as an image"
    server.post("/content/module/qr", name="Guest", opt_kind="wifi", opt_ssid="Guest net", opt_password="hunter22", opt_security="WPA")
    guest = server.ids()["Guest"]
    assert server.client.get(f"/api/module/{guest}/data").json["svg"].startswith("<svg")
    options = server.client.get(f"/api/module/{guest}/config").json["options"]
    assert "password" not in options and "ssid" not in options, "Wi-Fi details reach the page only inside the code"
    server.post("/content/module/qr", name="Empty", opt_kind="text", opt_content="")
    assert server.client.get(f"/api/module/{server.ids()['Empty']}/data").status_code == 400


def test_sun_and_air_shapes(monkeypatch):
    monkeypatch.setattr(modules, "_geocode", lambda location: (52.13, -0.99, "Towcester"))
    replies = {
        "api.open-meteo.com": {"daily": {"time": ["2026-10-05", "2026-10-06"], "sunrise": ["2026-10-05T07:10", "2026-10-06T07:12"],
                                         "sunset": ["2026-10-05T18:31", "2026-10-06T18:29"], "daylight_duration": [40860.4, 40620.0]}},
        "air-quality-api.open-meteo.com": {"current": {"european_aqi": 34, "pm10": 12.1, "pm2_5": 7.4, "uv_index": 2.3,
                                                        "grass_pollen": 0.0, "birch_pollen": None, "ragweed_pollen": 3.5}},
    }
    monkeypatch.setattr(modules, "_get", lambda url, headers=None: (
        json.dumps(replies[url.split("/")[2]]).encode(), "application/json"))
    sun = modules.fetch_sun({"location": "Towcester"}, 1)
    assert sun["place"] == "Towcester" and sun["days"][0] == {
        "date": "2026-10-05", "sunrise": "2026-10-05T07:10", "sunset": "2026-10-05T18:31", "daylight_s": 40860}
    air = modules.fetch_air({"location": "Towcester"}, 1)
    assert (air["aqi"], air["uv"]) == (34, 2.3) and air["pollen"] == {"grass": 0.0, "ragweed": 3.5}
    with pytest.raises(modules.ServiceError):
        modules.fetch_sun({"location": ""}, 1)
