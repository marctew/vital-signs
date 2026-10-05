"""Modules: per-item settings, secrets staying on the server, and the data providers' parsing."""
import json
from datetime import datetime, timedelta

from server import modules


def test_modules_are_offered_and_not_auto_added_as_pages(server):
    page = server.client.get("/content").text
    for name in ("Clock", "Calendar", "RSS feed", "Plex now playing", "Weather", "Frigate cameras", "Service status",
                 "World clocks", "Countdown", "Notice", "Image", "Photo frame", "Board", "Sun and moon", "Air quality", "QR code"):
        assert f">{name}</a>" in page, name
    assert not {"clock", "rss", "weather", "plex", "calendar", "frigate"} & set(server.ids())
    form = server.client.get("/content?module=weather").text
    assert "Add: Weather" in form and 'name="opt_location"' in form


def test_same_module_twice_with_different_settings(server):
    server.post("/content/module/clock", name="London", opt_title="London", opt_tz="Europe/London", opt_date="on")
    server.post("/content/module/clock", name="New York", opt_tz="America/New_York", opt_hour12="on", opt_seconds="on")
    london, new_york = server.ids()["London"], server.ids()["New York"]
    assert server.client.get(f"/api/module/{london}/config").json == {"module": "clock", "options": {
        "title": "London", "tz": "Europe/London", "hour12": False, "seconds": False, "date": True,
        "background": "#0b0f14", "accent": "#4cc2ff"}}
    options = server.client.get(f"/api/module/{new_york}/config").json["options"]
    assert options["hour12"] is True and options["date"] is False
    assert server.client.get(f"/api/module/{london}/data").status_code == 404, "a clock has no data"
    url = server.db().execute("SELECT url FROM content_items WHERE id = ?", (london,)).fetchone()[0]
    assert url == f"/pages/clock/?m={london}" and server.client.get(url).status_code == 200
    server.post(f"/content/{london}/module/clock", name="London", opt_title="LDN")
    assert server.client.get(f"/api/module/{london}/config").json["options"]["title"] == "LDN"
    assert "is required" in server.post("/content/module/weather", name="W", opt_location="").text


def test_secrets_and_private_settings_never_reach_the_page(server):
    server.post("/content/module/plex", name="Plex", opt_server="http://plex.lan:32400", opt_token="SECRET",
                opt_users="marc\nSam ", opt_paused="on")
    plex = server.ids()["Plex"]
    options = server.client.get(f"/api/module/{plex}/config").json["options"]
    assert "token" not in options and "server" not in options and options["users"] == ["marc", "Sam"]
    assert "SECRET" not in server.client.get(f"/content/{plex}").text
    server.post(f"/content/{plex}/module/plex", name="Plex", opt_server="http://plex.lan:32400", opt_token="", opt_users="marc")
    stored = json.loads(server.db().execute("SELECT config FROM content_items WHERE id = ?", (plex,)).fetchone()[0])
    assert stored["token"] == "SECRET", "a blank token field keeps the stored one"
    for path in ("/etc/passwd", "/library/../../x", "/library/metadata/1/thumb/2;rm"):
        assert server.client.get(f"/api/module/{plex}/image", query_string={"path": path}).status_code == 404, path


def test_rss_and_atom_parsing():
    rss = b"""<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Example News</title>
    <item><title>First &amp; best</title><link>https://e.example/1</link><description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description>
    <pubDate>Sun, 04 Oct 2026 10:00:00 GMT</pubDate><media:thumbnail url="https://e.example/1.jpg"/></item>
    <item><title>Second</title><link>https://e.example/2</link><pubDate>Sun, 04 Oct 2026 12:00:00 +0100</pubDate></item></channel></rss>"""
    items = modules.parse_feed(rss)
    assert items[0] == {"source": "Example News", "title": "First & best", "link": "https://e.example/1", "summary": "Hello world",
                        "published": "2026-10-04T10:00:00+00:00", "image": "https://e.example/1.jpg"}
    assert items[1]["published"] == "2026-10-04T11:00:00+00:00"
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Blog</title><entry><title>Post</title>
    <link href="https://b.example/p"/><updated>2026-10-04T09:30:00Z</updated><summary>Sum</summary></entry></feed>"""
    assert modules.parse_feed(atom)[0]["link"] == "https://b.example/p"


def test_calendar_expands_repeating_events():
    today = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    stamp = lambda d: d.strftime("%Y%m%dT%H%M%S")
    ics = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//EN", "X-WR-CALNAME:Family",
        "BEGIN:VEVENT", "UID:1", "DTSTAMP:20260101T000000Z",
        f"DTSTART:{stamp(today - timedelta(days=30))}", f"DTEND:{stamp(today - timedelta(days=30) + timedelta(hours=1))}",
        "RRULE:FREQ=DAILY", "SUMMARY:Standup", "LOCATION:Kitchen", "END:VEVENT",
        "BEGIN:VEVENT", "UID:2", "DTSTAMP:20260101T000000Z",
        f"DTSTART;VALUE=DATE:{(today + timedelta(days=2)).strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{(today + timedelta(days=3)).strftime('%Y%m%d')}",
        "SUMMARY:Bin day", "END:VEVENT", "END:VCALENDAR", ""]).encode()
    events = modules.parse_calendar(ics, today - timedelta(days=1), today + timedelta(days=4))
    standups = [e for e in events if e["title"] == "Standup"]
    assert len(standups) == 5 and standups[0]["location"] == "Kitchen" and standups[0]["all_day"] is False
    bins = [e for e in events if e["title"] == "Bin day"]
    assert len(bins) == 1 and bins[0]["all_day"] and bins[0]["calendar"] == "Family"


def test_plex_sessions_and_user_filter():
    payload = {"MediaContainer": {"Metadata": [
        {"type": "episode", "title": "Pilot", "grandparentTitle": "The Show", "parentIndex": 1, "index": 2, "duration": 1000,
         "viewOffset": 250, "summary": "It <b>begins</b>.", "grandparentThumb": "/library/metadata/5/thumb/9",
         "User": {"title": "Marc"}, "Player": {"state": "playing", "title": "TV"}},
        {"type": "movie", "title": "A Film", "year": 1999, "thumb": "/library/metadata/7/thumb/1",
         "User": {"title": "Guest"}, "Player": {"state": "paused"}}]}}
    assert modules.parse_plex_sessions(payload, ["marc"], 12) == [{
        "user": "Marc", "type": "episode", "title": "The Show", "subtitle": "S1 E2 · Pilot", "summary": "It begins .",
        "state": "playing", "player": "TV", "progress": 0.25, "image": "/api/module/12/image?path=/library/metadata/5/thumb/9"}]
    assert len(modules.parse_plex_sessions(payload, [], 12)) == 2
    assert modules.parse_plex_sessions({"MediaContainer": {}}, [], 1) == []


def test_plex_recently_added(monkeypatch):
    calls = []

    def fake_get(url, headers=None):
        calls.append(url)
        reply = lambda data: (json.dumps(data).encode(), "application/json")
        if url.endswith("/status/sessions"):
            return reply({"MediaContainer": {}})
        if url.endswith("/library/sections"):
            return reply({"MediaContainer": {"Directory": [
                {"key": "1", "title": "Films", "type": "movie"}, {"key": "2", "title": "TV Shows", "type": "show"},
                {"key": "3", "title": "Photos", "type": "photo"}]}})
        if "/sections/1/" in url:
            return reply({"MediaContainer": {"Metadata": [
                {"type": "movie", "title": "A Film", "thumb": "/library/metadata/1/thumb/5", "addedAt": 300}]}})
        return reply({"MediaContainer": {"Metadata": [
            {"type": "episode", "grandparentTitle": "The Show", "grandparentThumb": "/library/metadata/9/thumb/2", "addedAt": 500},
            {"type": "episode", "grandparentTitle": "The Show", "grandparentThumb": "/library/metadata/9/thumb/2", "addedAt": 450}]}})

    monkeypatch.setattr(modules, "_get", fake_get)
    monkeypatch.setattr(modules, "_recent", {})
    cfg = {"server": "http://plex:32400", "token": "t", "idle_mode": "recent", "libraries": []}
    assert [i["title"] for i in modules.fetch_plex(cfg, 16)["recent"]] == ["The Show", "A Film"]
    assert not any("/sections/3/" in c for c in calls), "photo libraries are skipped"
    assert [i["title"] for i in modules.fetch_plex({**cfg, "libraries": ["films"]}, 16)["recent"]] == ["A Film"]
    before = len(calls)
    modules.fetch_plex(cfg, 16)
    assert len(calls) == before + 1, "recently added is cached; only sessions is fetched again"
    assert "recent" not in modules.fetch_plex({**cfg, "idle_mode": "text"}, 16)


def test_frigate_camera_list():
    frigate = {
        "go2rtc": {"streams": {"front_door": [], "drive_sub": []}},
        "cameras": {
            "front_door": {"enabled": True, "detect": {"width": 1280, "height": 720}, "live": {"stream_name": "front_door"}},
            "drive": {"detect": {"width": 640, "height": 360}, "live": {"streams": {"Main": "drive_main", "Sub": "drive_sub"}}},
            "garden": {"detect": {"width": 1920, "height": 1080}, "live": {"stream_name": "garden"}},
            "old_cam": {"enabled": False},
        },
    }
    cameras = modules.parse_frigate_config(frigate, [])
    assert [c["name"] for c in cameras] == ["front_door", "drive", "garden"]
    assert cameras[0] == {"name": "front_door", "stream": "front_door", "width": 1280, "height": 720}
    assert cameras[1]["stream"] == "drive_sub" and cameras[2]["stream"] is None
    assert [c["name"] for c in modules.parse_frigate_config(frigate, ["Garden", "front_door", "nope"])] == ["garden", "front_door"]
