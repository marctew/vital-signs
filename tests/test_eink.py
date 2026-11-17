"""E-ink displays: what is current, when the device wakes, the pictures, and Home Assistant."""
import io
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta

import pytest
from PIL import Image

from server import db as database
from server import eink, ha_mqtt, services
from tests.test_device_api import MAC, Device

BASE = "http://server"


def add(server, name, **fields):
    """Add a URL content item whose address is made from its name."""
    return server.add_content(name, url=f"https://{name.lower().replace(' ', '-')}.example", **fields)


def pixels(image):
    image = image.convert("RGB")
    return [image.getpixel((x, y)) for y in range(image.height) for x in range(image.width)]


class FakeBrowser:
    """Stands in for Chromium: every page is a flat colour, set per test."""

    def __init__(self):
        self.colour = (255, 255, 255)
        self.fail = None
        self.sources = []
        self.calls = []

    def shoot(self, url, width, height, zoom=1.0, css=""):
        self.calls.append({"url": url, "width": width, "height": height, "zoom": zoom, "css": css})
        if self.fail:
            raise eink.RenderError(self.fail)
        out = io.BytesIO()
        Image.new("RGB", (width, height), self.colour).save(out, "PNG")
        return out.getvalue(), self.sources


class Panel:
    """An adopted device with a playlist, a fake browser and a clock the test moves."""

    def __init__(self, server, monkeypatch):
        self.server = server
        self.now = time.time()
        monkeypatch.setattr(time, "time", lambda: self.now)
        self.device = Device(server)
        self.id = self.device.adopt()
        self.browser = FakeBrowser()
        self.conn = database.connect(server.app.config["DB_PATH"])

    def playlist(self, *names, duration=600, name="Main"):
        self.server.post("/playlists", name=name)
        playlist_id = self.server.ids("playlists")[name]
        for item in names:
            if item not in self.server.ids():
                add(self.server, item)
            self.server.post(f"/playlists/{playlist_id}/items", content_id=self.server.ids()[item], duration=duration)
        return playlist_id

    def show(self, *names, duration=600):
        self.server.post(f"/displays/{self.id}/assign", playlist_id=self.playlist(*names, duration=duration))

    def render(self):
        return eink.render_due(self.conn, self.server.data_dir, BASE, self.browser, self.now)

    def wake(self, **headers):
        r = self.device.wake(**headers)
        assert r.status_code == 200, r.get_data(as_text=True)
        return r.json

    def row(self, key):
        row = eink.render_row(self.conn, self.id, str(key))
        return dict(row) if row else None

    def picture(self, filename):
        return Image.open(eink.image_path(self.server.data_dir, self.id, filename))

    def display(self):
        return self.server.client.get(f"/api/v1/displays/{self.id}").json


@pytest.fixture
def panel(server, monkeypatch):
    p = Panel(server, monkeypatch)
    yield p
    p.conn.close()


# --- schedules on the server's clock ------------------------------------------------

MONDAY = datetime(2026, 11, 16)     # a Monday


def test_period_active_handles_days_and_midnight():
    work = {"on": "09:00", "off": "17:00", "days": [0, 1, 2, 3, 4]}
    assert eink.period_active(work, MONDAY.replace(hour=9)) and eink.period_active(work, MONDAY.replace(hour=16, minute=59))
    assert not eink.period_active(work, MONDAY.replace(hour=17)) and not eink.period_active(work, MONDAY.replace(hour=8, minute=59))
    assert not eink.period_active(work, MONDAY + timedelta(days=5, hours=10))        # Saturday
    late = {"on": "22:00", "off": "03:00", "days": [4]}                                # Friday night
    assert eink.period_active(late, MONDAY + timedelta(days=4, hours=23))
    assert eink.period_active(late, MONDAY + timedelta(days=5, hours=2)), "it runs on into Saturday morning"
    assert not eink.period_active(late, MONDAY + timedelta(days=5, hours=3))
    assert not eink.period_active(late, MONDAY + timedelta(days=4, hours=2)), "Friday morning is before it starts"


def test_next_boundary_finds_the_next_start_or_end():
    work = {"on": "09:00", "off": "17:00", "days": [0, 1, 2, 3, 4]}
    assert eink.next_boundary([work], MONDAY.replace(hour=8)) == 3600
    assert eink.next_boundary([work], MONDAY.replace(hour=16)) == 3600
    assert eink.next_boundary([work], MONDAY.replace(hour=17)) == 16 * 3600            # Tuesday 09:00
    assert eink.next_boundary([work], MONDAY + timedelta(days=4, hours=18)) == (2 * 24 + 15) * 3600   # Friday evening to Monday
    late = {"on": "22:00", "off": "03:00", "days": [4]}
    assert eink.next_boundary([late], MONDAY + timedelta(days=4, hours=23)) == 4 * 3600
    assert eink.next_boundary([], MONDAY) is None


# --- pictures -----------------------------------------------------------------------

def test_pure_inks_survive_and_nothing_else_does():
    image = Image.new("RGB", (4, 1))
    inks = [(0, 0, 0), (255, 255, 255), (255, 0, 0), (255, 255, 0)]
    for x, ink in enumerate(inks):
        image.putpixel((x, 0), ink)
    assert pixels(eink.quantise(image, "bwry")) == inks
    odd = Image.new("RGB", (3, 1))
    for x, colour in enumerate([(40, 40, 40), (230, 230, 230), (200, 30, 30)]):
        odd.putpixel((x, 0), colour)
    assert pixels(eink.quantise(odd, "bwry")) == [(0, 0, 0), (255, 255, 255), (255, 0, 0)]
    assert pixels(eink.quantise(odd, "bw"))[2] in ((0, 0, 0), (255, 255, 255))


def shares(image):
    """{ink: fraction of the picture} for a quantised image."""
    counts = image.convert("RGB").getcolors()
    total = sum(n for n, _ in counts)
    return {ink: n / total for n, ink in counts}


def patches(*colours, size=60):
    """A test picture of flat patches side by side, with black and white patches so the tones are not stretched."""
    colours = [(0, 0, 0), (255, 255, 255), *colours]
    image = Image.new("RGB", (size * len(colours), size))
    for n, colour in enumerate(colours):
        image.paste(colour, (n * size, 0, (n + 1) * size, size))
    return image, lambda quantised, n: quantised.crop(((n + 2) * size + 8, 8, (n + 3) * size - 8, size - 8))


BLACK, WHITE, RED, YELLOW = (0, 0, 0), (255, 255, 255), (255, 0, 0), (255, 255, 0)


def test_dither_mixes_inks_where_flat_does_not():
    image, patch = patches((128, 128, 128))
    assert len(patch(eink.quantise(image, "bwry", "none"), 0).getcolors()) == 1
    mixed = shares(patch(eink.quantise(image, "bwry", "dither"), 0))
    assert set(mixed) == {BLACK, WHITE}, "a grey is made of black and white only, never red or yellow"
    assert 0.2 < mixed[BLACK] < 0.6


def test_dithered_solid_colours_stay_solid():
    image, patch = patches((200, 40, 45), (245, 205, 40))       # a red jumper, a yellow ball
    out = eink.quantise(image, "bwry", "dither")
    assert shares(patch(out, 0)).get(RED, 0) > 0.97, "no black specks in a red"
    assert shares(patch(out, 1)).get(YELLOW, 0) > 0.97, "no red specks in a yellow"


def test_dithered_skies_and_highlights_are_not_peppered():
    image, patch = patches((170, 200, 235), (240, 240, 238), (226, 182, 152))    # sky, a cloud, skin
    out = eink.quantise(image, "bwry", "dither")
    plain = image.quantize(palette=eink._palette_image("bwry"), dither=Image.Dither.FLOYDSTEINBERG)
    sky, before = shares(patch(out, 0)).get(BLACK, 0), shares(patch(plain, 0)).get(BLACK, 0)
    assert sky < 0.18 and sky < before * 0.8, "a light sky has fewer black dots than plain Floyd-Steinberg gives"
    assert shares(patch(out, 1)).get(WHITE, 0) > 0.98, "near-white is white"
    skin = shares(patch(out, 2))
    assert skin.get(BLACK, 0) < 0.03 and skin.get(YELLOW, 0) + skin.get(RED, 0) > 0.15, "skin is warm, not grey"


def test_faint_colours_are_dithered_as_greys():
    image, patch = patches((161, 125, 124), (230, 213, 201), (213, 167, 142))     # a warm-grey sofa, off-white fur, tan fur
    out = eink.quantise(image, "bwry", "dither")
    sofa, white, tan = (shares(patch(out, n)) for n in range(3))
    assert sofa.get(RED, 0) + sofa.get(YELLOW, 0) < 0.12, "a warm grey is not sprinkled with red"
    assert white.get(WHITE, 0) > 0.9 and white.get(BLACK, 0) < 0.05, "off-white is nearly clean white"
    assert tan.get(RED, 0) + tan.get(YELLOW, 0) > 0.2, "a real colour keeps its colour"


def test_dither_output_is_an_indexed_picture_in_pure_inks():
    image, _ = patches((120, 160, 90))
    out = eink.quantise(image, "bwry", "dither")
    assert out.mode == "P" and out.size == image.size and set(shares(out)) <= {BLACK, WHITE, RED, YELLOW}
    assert eink.png_bytes(out, "bwry")[24] == 2
    mono = eink.quantise(image, "bw", "dither")
    assert set(shares(mono)) <= {BLACK, WHITE}


def test_png_is_indexed_at_the_palettes_depth():
    image = eink.quantise(Image.new("RGB", (800, 480), (255, 0, 0)), "bwry")
    data = eink.png_bytes(image, "bwry")
    assert data[24] == 2 and data[25] == 3, "2 bits per pixel, palette colour type"
    assert Image.open(io.BytesIO(data)).size == (800, 480)
    assert eink.png_bytes(eink.quantise(Image.new("RGB", (8, 8)), "bw"), "bw")[24] == 1
    assert eink.palette_name("og_4clr") == "bwry" and eink.palette_name("og") == "bw" and eink.palette_name("who knows") == "bw"


def test_battery_percent():
    assert [eink.battery_percent(v) for v in (4.2, 4.1, 3.55, 3.0, 2.8, 0, None, "x")] == [100, 100, 50, 0, 0, None, None, None]


# --- what is current ----------------------------------------------------------------

def test_one_item_is_drawn_and_served(panel):
    panel.show("News")
    first = panel.wake()
    assert first["refresh_rate"] == 300, "nothing can draw in this test, so it does not hurry back"
    assert panel.render() == 1
    call = panel.browser.calls[0]
    assert (call["url"], call["width"], call["height"]) == ("https://news.example", 800, 480 - eink.FOOTER_PX)
    reply = panel.wake()
    row = panel.row(panel.server.ids()["News"])
    assert reply["filename"] == row["filename"] and reply["refresh_rate"] == 3600
    picture = panel.picture(reply["filename"])
    assert picture.size == (800, 480) and picture.mode == "P"
    d = panel.display()
    assert d["current"] == {"name": "News", "override": False} and d["screenshot_url"].startswith(f"/eink/{panel.id}.png")
    assert panel.server.client.get(d["screenshot_url"]).data.startswith(b"\x89PNG")


def test_local_pages_are_told_they_are_on_eink(panel):
    panel.show("Clock")
    conn = panel.conn
    conn.execute("UPDATE content_items SET url = '/pages/clock/?tz=Europe/London', zoom = 1.5, css = 'b{}' WHERE name = 'Clock'")
    conn.commit()
    panel.render()
    call = panel.browser.calls[0]
    assert call["url"] == (f"{BASE}/pages/clock/?tz=Europe%2FLondon&display=panel&width=800&height=480"
                           "&orientation=landscape&medium=eink&palette=bwry")
    assert call["zoom"] == 1.5 and call["css"] == "b{}"


def test_a_playlist_rotates_on_the_clock(panel):
    panel.show("A", "B", "C", duration=600)
    ids = panel.server.ids()
    panel.now = 1_800_000_000 - 1_800_000_000 % 1800          # the start of a full turn of the playlist
    seen = []
    for minute in (0, 9, 10, 25, 30):
        item = eink.current(panel.conn, panel.id, panel.now + minute * 60, 300)
        seen.append((item["name"], round(item["until"] - panel.now) // 60, item["next"]["name"]))
    assert seen == [("A", 10, "B"), ("A", 10, "B"), ("B", 20, "C"), ("C", 30, "A"), ("A", 40, "B")]
    assert eink.current(panel.conn, panel.id, panel.now, 300)["content_id"] == ids["A"]


def test_a_turn_is_never_shorter_than_the_shortest_sleep(panel):
    panel.show("A", "B", duration=30)
    panel.now = 1_800_000_000 - 1_800_000_000 % 600
    item = eink.current(panel.conn, panel.id, panel.now + 60, 300)
    assert item["name"] == "A" and round(item["until"] - panel.now) == 300


def test_the_next_item_is_drawn_ahead_of_its_turn(panel):
    panel.show("A", "B", duration=1200)
    panel.now = 1_800_000_000 - 1_800_000_000 % 2400
    panel.render()
    assert [c["url"] for c in panel.browser.calls] == ["https://a.example"]
    panel.now += 1200 - 200            # B's turn is within the shortest sleep
    panel.render()
    assert [c["url"] for c in panel.browser.calls] == ["https://a.example", "https://b.example"]
    reply = panel.wake()
    assert reply["filename"] == panel.row(panel.server.ids()["A"])["filename"] and reply["refresh_rate"] == 300
    panel.now += 300
    assert panel.wake()["filename"] == panel.row(panel.server.ids()["B"])["filename"]


def test_override_beats_rule_beats_assignment(panel):
    panel.show("Usual")
    evening = panel.playlist("Evening news", name="Evening")
    now = datetime.fromtimestamp(panel.now)
    on, off = (now - timedelta(minutes=5)).strftime("%H:%M"), (now + timedelta(minutes=30)).strftime("%H:%M")
    panel.server.post("/schedules", name="Now", on=on, off=off, days=[str(d) for d in range(7)])
    schedule_id = panel.server.ids("power_schedules")["Now"]
    assert eink.current(panel.conn, panel.id, panel.now)["name"] == "Usual"
    panel.server.post(f"/displays/{panel.id}/playlist-rules", schedule_id=schedule_id, playlist_id=evening)
    item = eink.current(panel.conn, panel.id, panel.now)
    assert (item["name"], item["source"]) == ("Evening news", "rule")
    panel.server.client.post(f"/api/v1/displays/{panel.id}/override", json={"url": "https://alert.example", "minutes": 5})
    item = eink.current(panel.conn, panel.id, panel.now)
    assert (item["name"], item["source"], item["content_id"]) == ("https://alert.example", "override", None)
    assert round(item["until"] - panel.now) == 300
    panel.now += 301
    assert eink.current(panel.conn, panel.id, panel.now)["name"] == "Evening news"


# --- overrides by content item --------------------------------------------------------

def test_show_a_content_item_on_demand(panel):
    panel.show("Usual")
    add(panel.server, "Meal plan", zoom="1.25", css="h1{}")
    panel.render()
    panel.wake()
    r = panel.server.client.post(f"/api/v1/displays/trmnl-00180c:panel/override", json={"content": "meal PLAN", "minutes": 10})
    assert r.status_code == 200 and r.json["override"]["content"] == "Meal plan"
    assert panel.render() == 1
    call = panel.browser.calls[-1]
    assert (call["url"], call["zoom"], call["css"]) == ("https://meal-plan.example", 1.25, "h1{}")
    reply = panel.wake()
    assert reply["filename"] == panel.row(panel.server.ids()["Meal plan"])["filename"]
    assert reply["refresh_rate"] == 600, "it wakes again as the override ends"
    assert panel.display()["current"] == {"name": "Meal plan", "override": True}
    panel.now += 601
    assert panel.wake()["filename"] == panel.row(panel.server.ids()["Usual"])["filename"]


def test_an_unknown_content_item_is_a_404(panel):
    r = panel.server.client.post(f"/api/v1/displays/{panel.id}/override", json={"content": "Nope"})
    assert r.status_code == 404 and "No such content item" in r.json["error"]
    assert panel.server.client.get("/api/v1/content").json["content"] == []


def test_a_content_override_on_a_pi_carries_its_zoom_and_css(server):
    server.poll()
    content_id = add(server, "Meal plan", zoom="1.5", css="h1{}")
    r = server.client.post("/api/v1/displays/pi:Left/override", json={"content_id": content_id, "minutes": 5})
    assert r.status_code == 200
    override = server.poll()["displays"]["HDMI-A-1"]["override"]
    assert (override["url"], override["zoom"], override["css"], override["local"]) == ("https://meal-plan.example", 1.5, "h1{}", False)
    server.client.post("/api/v1/displays/pi:Left/override", json={"url": "https://x.example", "minutes": 5})
    assert "zoom" not in server.poll()["displays"]["HDMI-A-1"]["override"]


def test_the_override_form_takes_a_content_item(panel):
    content_id = add(panel.server, "Meal plan")
    panel.server.post(f"/displays/{panel.id}/override", content_id=str(content_id), url="", minutes="10")
    assert panel.display()["override"]["content"] == "Meal plan"


# --- when it wakes ----------------------------------------------------------------------

def test_wake_interval_follows_the_items_refresh(panel):
    panel.show("News")
    panel.conn.execute("UPDATE content_items SET refresh_interval = 900")
    panel.conn.commit()
    panel.render()
    assert panel.wake()["refresh_rate"] == 900 + eink.RENDER_MARGIN
    panel.now += 800
    assert panel.wake()["refresh_rate"] == 300, "never shorter than the shortest sleep"
    panel.conn.execute("UPDATE content_items SET eink_refresh = 1800")       # the e-ink setting wins
    panel.conn.commit()
    assert panel.wake()["refresh_rate"] == 1800 - 800 + eink.RENDER_MARGIN


def test_mains_power_allows_a_one_minute_wake(panel):
    panel.show("News")
    panel.server.post(f"/displays/{panel.id}/eink", name="panel", mains="on", footer="on")
    panel.server.client.post(f"/api/v1/displays/{panel.id}/override", json={"url": "https://x.example", "minutes": 1})
    panel.render()
    assert panel.wake()["refresh_rate"] == 60
    assert panel.display()["eink"]["battery_percent"] is None, "battery readings mean nothing on mains"


def test_a_picture_being_drawn_brings_it_straight_back(panel, monkeypatch):
    panel.show("News")
    monkeypatch.setattr(eink, "renderer_alive", lambda now: True)
    assert panel.wake()["refresh_rate"] == eink.PENDING_RATE


def test_outside_wake_hours_it_sleeps_until_they_start(panel):
    panel.show("News")
    panel.render()
    shown = panel.wake()["filename"]
    now = datetime.fromtimestamp(panel.now)
    on, off = (now + timedelta(hours=2)).strftime("%H:%M"), (now + timedelta(hours=3)).strftime("%H:%M")
    panel.server.post("/schedules", name="Later", on=on, off=off, days=[str(d) for d in range(7)])
    panel.server.post(f"/displays/{panel.id}/power", schedule_ids=str(panel.server.ids("power_schedules")["Later"]))
    reply = panel.wake()
    assert reply["filename"] == shown, "the panel keeps its picture"
    assert 2 * 3600 - 120 <= reply["refresh_rate"] <= 2 * 3600 + 10
    assert panel.display()["eink"]["awake"] is False
    panel.server.client.post(f"/api/v1/displays/{panel.id}/override", json={"url": "https://alert.example", "minutes": 5})
    panel.render()
    assert panel.wake()["filename"] != shown, "an override still gets through"


def test_wake_interval_stops_at_a_schedule_boundary(panel):
    panel.show("News")
    panel.render()
    now = datetime.fromtimestamp(panel.now)
    on, off = (now - timedelta(minutes=10)).strftime("%H:%M"), (now + timedelta(minutes=20)).strftime("%H:%M")
    panel.server.post("/schedules", name="Now", on=on, off=off, days=[str(d) for d in range(7)])
    panel.server.post(f"/displays/{panel.id}/power", schedule_ids=str(panel.server.ids("power_schedules")["Now"]))
    assert 19 * 60 - 61 <= panel.wake()["refresh_rate"] <= 20 * 60


def test_online_means_it_made_its_last_wake(panel):
    panel.show("News")
    panel.render()
    panel.wake()                                   # told to sleep an hour
    assert panel.display()["online"] is True
    panel.now += 3600 + 30 + 3600 - 10
    assert panel.display()["online"] is True
    panel.now += 20
    assert panel.display()["online"] is False


# --- unchanged pictures, the footer and stale data ------------------------------------------

def test_an_unchanged_picture_is_not_republished_before_the_heartbeat(panel):
    panel.show("News")
    panel.conn.execute("UPDATE content_items SET refresh_interval = 600")
    panel.conn.commit()
    assert panel.render() == 1
    key = panel.server.ids()["News"]
    first = panel.row(key)
    panel.now += 700
    assert panel.render() == 0 and len(panel.browser.calls) == 2, "drawn again, found the same, left alone"
    again = panel.row(key)
    assert again["filename"] == first["filename"] and again["checked_at"] == panel.now and again["published_at"] == first["published_at"]
    panel.now += 3000                              # past the hourly heartbeat
    assert panel.render() == 1
    assert panel.row(key)["filename"] != first["filename"], "the footer's time moved"


def test_a_changed_page_is_published_at_once(panel):
    panel.show("News")
    panel.conn.execute("UPDATE content_items SET refresh_interval = 600")
    panel.conn.commit()
    panel.render()
    first = panel.row(panel.server.ids()["News"])["filename"]
    panel.browser.colour = (255, 0, 0)
    panel.now += 700
    assert panel.render() == 1
    picture = panel.picture(panel.row(panel.server.ids()["News"])["filename"]).convert("RGB")
    assert picture.getpixel((400, 200)) == (255, 0, 0) and picture.getpixel((400, 479)) == (255, 255, 255)
    assert panel.row(panel.server.ids()["News"])["filename"] != first


def test_draw_again_republishes(panel):
    panel.show("News")
    panel.render()
    first = panel.row(panel.server.ids()["News"])["filename"]
    panel.now += 120
    panel.server.post(f"/displays/{panel.id}/eink/refresh")
    assert panel.render() == 1 and panel.row(panel.server.ids()["News"])["filename"] != first
    assert panel.server.client.post(f"/api/v1/displays/{panel.id}/refresh").status_code == 200


def test_the_footer_can_be_turned_off(panel):
    panel.show("News")
    panel.server.post(f"/displays/{panel.id}/eink", name="panel")       # footer unticked
    panel.browser.colour = (0, 0, 0)
    panel.render()
    assert panel.browser.calls[0]["height"] == 480
    assert panel.picture(panel.row(panel.server.ids()["News"])["filename"]).convert("RGB").getpixel((400, 479)) == (0, 0, 0)


def test_footer_text():
    dev = {"telemetry": json.dumps({"battery_v": 3.1, "rssi": -60}), "mains": 0}
    item = {"name": "Meal plan", "content_id": 3, "url": "/pages/meals/?m=3"}
    at = datetime(2026, 11, 17, 14, 32).timestamp()
    info = eink.footer_info(dev, item, at, False, [{"label": "Meals", "at": at - 3600}], at)
    assert info == {"left": "Checked 14:32  Meals 13:32", "name": "Meal plan", "right": "Battery 9%  Wi-Fi good",
                    "stale": False, "low": True}
    stale = eink.footer_info({**dev, "mains": 1}, {**item, "content_id": None, "url": "https://bbc.co.uk/news"}, at - 7200, True, [], at)
    assert (stale["left"], stale["name"], stale["right"], stale["stale"]) == ("Data from 12:32", "bbc.co.uk", "", True)
    assert eink.footer_info(dev, item, 0, True, [], at)["left"] == "No data yet"
    assert eink.footer_image(800, "bwry", info).size == (800, eink.FOOTER_PX)


def test_a_page_that_fails_keeps_the_old_picture_and_is_flagged(panel):
    panel.show("News")
    panel.conn.execute("UPDATE content_items SET refresh_interval = 600")
    panel.conn.commit()
    panel.render()
    good = panel.wake()["filename"]
    panel.browser.fail = "HTTP 502"
    panel.now += 700
    assert panel.render() == 0
    assert panel.wake()["filename"] == good
    d = panel.display()
    assert d["errors"] == [{"name": "News", "url": "https://news.example", "error": "HTTP 502"}]
    assert d["eink"]["stale"] is True
    calls = len(panel.browser.calls)
    panel.now += 30
    panel.render()
    assert len(panel.browser.calls) == calls, "not retried at once"
    panel.browser.fail = None
    panel.now += eink.RETRY_FAILED
    panel.render()
    assert panel.display()["errors"] == [] and panel.display()["eink"]["stale"] is False


def test_a_page_that_never_drew_says_so_on_the_panel(panel):
    panel.show("News")
    panel.browser.fail = "it did not answer in time"
    panel.render()
    reply = panel.wake()
    assert reply["refresh_rate"] == 300 and eink.image_path(panel.server.data_dir, panel.id, reply["filename"])


def test_module_data_that_stops_arriving_turns_the_picture_stale(panel, monkeypatch):
    from server import modules
    monkeypatch.setattr(modules, "fetch_meals", lambda cfg, content_id: {"days": [], "empty": True})
    panel.server.post("/content/module/meals", name="Meal plan", opt_url="http://homeform.lan", opt_token="t", opt_days="5", opt_slots="both")
    content_id = panel.server.ids()["Meal plan"]
    panel.server.post(f"/displays/{panel.id}/assign", playlist_id=panel.playlist("Meal plan"))
    cache = panel.server.data_dir / "module-cache"
    cache.mkdir(exist_ok=True)
    saved = cache / f"{content_id}-abc.json"
    saved.write_text("{}")
    os.utime(saved, (panel.now - 60, panel.now - 60))
    panel.render()
    row = panel.row(content_id)
    assert row["stale"] == 0 and round(row["data_at"]) == round(panel.now - 60)
    fresh = row["filename"]
    panel.now += 3 * 3600                              # three hours with no successful fetch
    assert panel.render() == 1, "going stale changes the picture even though the page looks the same"
    row = panel.row(content_id)
    assert row["stale"] == 1 and row["filename"] != fresh
    panel.wake()
    assert panel.display()["eink"]["stale"] is True


def test_a_pushed_board_is_redrawn_without_waiting(panel):
    panel.server.post("/content/module/board", name="Jobs")
    panel.server.post(f"/displays/{panel.id}/assign", playlist_id=panel.playlist("Jobs"))
    panel.render()
    calls = len(panel.browser.calls)
    panel.now += 60
    panel.render()
    assert len(panel.browser.calls) == calls
    panel.server.client.post("/api/v1/boards/Jobs", json={"value": 3})
    panel.render()
    assert len(panel.browser.calls) == calls + 1


def test_keep_ready_items_are_drawn_for_every_eink_display(panel):
    panel.show("Usual")
    content_id = add(panel.server, "Meal plan", eink_form="1", eink_ready="on", eink_dither="none", eink_refresh_minutes="30")
    row = panel.conn.execute("SELECT eink_ready, eink_dither, eink_refresh FROM content_items WHERE id = ?", (content_id,)).fetchone()
    assert tuple(row) == (1, "none", 1800)
    assert panel.render() == 2
    assert panel.row(content_id)["filename"]
    panel.server.client.post(f"/api/v1/displays/{panel.id}/override", json={"content_id": content_id})
    assert panel.wake()["filename"] == panel.row(content_id)["filename"], "shown at the very next wake, with nothing to draw"


def test_eink_options_only_change_when_the_form_shows_them(panel):
    content_id = add(panel.server, "Meal plan", eink_form="1", eink_ready="on")
    panel.server.post(f"/content/{content_id}", name="Meal plan", url="https://meal-plan.example", zoom="1", refresh_interval="0", css="")
    assert panel.conn.execute("SELECT eink_ready FROM content_items WHERE id = ?", (content_id,)).fetchone()[0] == 1
    page = panel.server.client.get(f"/content/{content_id}").get_data(as_text=True)
    assert "On e-ink displays" in page


def test_default_dither(panel):
    add(panel.server, "Site")
    panel.server.post("/content/module/notice", name="Note", opt_text="hello")
    rows = {r["name"]: eink.default_dither(r) for r in panel.conn.execute("SELECT * FROM content_items")}
    assert (rows["Site"], rows["Note"]) == ("dither", "none")


# --- Home Assistant ------------------------------------------------------------------------

def configs(messages, component):
    return {t.split("/")[2]: json.loads(p) for t, p in messages.items() if t.startswith(f"homeassistant/{component}/")}


def test_home_assistant_entities(panel):
    panel.show("News")
    add(panel.server, "Meal plan")
    panel.render()
    panel.wake(**{"Battery-Voltage": "3.55", "RSSI": "-70"})
    messages = ha_mqtt.build_messages(panel.conn, "homeassistant")
    uid = f"vitalsigns_display_{panel.id}"
    sensors, binary, selects = configs(messages, "sensor"), configs(messages, "binary_sensor"), configs(messages, "select")
    assert {f"{uid}_battery", f"{uid}_battery_v", f"{uid}_rssi", f"{uid}_last_seen", f"{uid}_next_due", f"{uid}_drawn",
            f"{uid}_showing", f"{uid}_firmware"} == set(sensors)
    assert {f"{uid}_online", f"{uid}_problem", f"{uid}_stale"} == set(binary), "no USB sensor: this board cannot tell"
    assert sensors[f"{uid}_battery"]["device_class"] == "battery" and sensors[f"{uid}_last_seen"]["device_class"] == "timestamp"
    options = selects[f"{uid}_show"]["options"]
    assert options[0] == "Playlist" and {"Meal plan", "News"} <= set(options) and "Main (rotating)" not in options
    assert set(configs(messages, "button")) == {f"{uid}_refresh"}
    assert not configs(messages, "switch"), "an e-ink panel has no screen to switch"
    assert not [t for t in messages if "vitalsigns_agent_" in t], "the device is one Home Assistant device, not two"
    state = json.loads(messages[f"vitalsigns/display/{panel.id}/state"])
    assert (state["online"], state["battery"], state["battery_v"], state["rssi"], state["showing"], state["show"], state["stale"]) == (
        "ON", 50, 3.55, -70, "News", "Playlist", "OFF")
    assert state["last_seen"].endswith("+00:00") and state["next_due"] > state["last_seen"]
    assert sensors[f"{uid}_battery"]["device"]["manufacturer"] == "TRMNL"


def test_home_assistant_show_select_and_buttons(panel):
    panel.show("News")
    add(panel.server, "Meal plan")
    panel.render()
    base = f"vitalsigns/display/{panel.id}"
    assert ha_mqtt.handle_command(panel.conn, f"{base}/set/show", "Meal plan") == "panel: show Meal plan"
    override = panel.display()["override"]
    assert override["content"] == "Meal plan" and override["remaining_s"] == ha_mqtt.SHOW_MINUTES * 60
    assert json.loads(ha_mqtt.build_messages(panel.conn, "homeassistant")[f"{base}/state"])["show"] == "Meal plan"
    assert ha_mqtt.handle_command(panel.conn, f"{base}/set/show", "Playlist") == "panel: show Playlist"
    assert panel.display()["override"] is None
    assert ha_mqtt.handle_command(panel.conn, f"{base}/set/show", "No such thing") is None
    assert ha_mqtt.handle_command(panel.conn, f"{base}/press/refresh", "PRESS") == "panel: draw again"
    assert ha_mqtt.handle_command(panel.conn, f"{base}/set/screen", "OFF") is None
    assert ha_mqtt.handle_command(panel.conn, f"{base}/press/reload", "PRESS") is None


def test_mains_power_drops_the_battery_entities(panel):
    panel.server.post(f"/displays/{panel.id}/eink", name="panel", mains="on", footer="on")
    panel.wake()
    sensors = configs(ha_mqtt.build_messages(panel.conn, "homeassistant"), "sensor")
    assert not [s for s in sensors if "battery" in s]


# --- the rest of the server is unchanged ------------------------------------------------------

def test_pi_displays_are_untouched(panel):
    panel.server.poll()
    displays = {d["ref"]: d for d in panel.server.client.get("/api/v1/displays").json["displays"]}
    assert displays["pi:Left"]["kind"] == "screen" and "eink" not in displays["pi:Left"]
    agents = {a["hostname"]: a["kind"] for a in panel.server.client.get("/api/v1/agents").json["agents"]}
    assert agents == {"pi": "pi", "trmnl-00180c": "eink"}
    assert panel.server.client.get("/").status_code == 200
    assert "E-ink" not in panel.server.client.get("/displays/2").get_data(as_text=True)


def test_an_old_database_is_upgraded(make_server):
    first = make_server()
    first.poll()
    add(first, "News")
    db = first.db()
    for table in ("eink_devices", "eink_logs", "eink_renders"):
        db.execute(f"DROP TABLE {table}")
    # A database from the first e-ink release: the devices table without the frame margins.
    db.execute("CREATE TABLE eink_devices (id INTEGER PRIMARY KEY, agent_id INTEGER UNIQUE, mac TEXT NOT NULL UNIQUE,"
               " api_key TEXT NOT NULL, friendly_id TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', fw_version TEXT NOT NULL DEFAULT '',"
               " width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0, first_seen REAL NOT NULL DEFAULT 0,"
               " last_seen REAL NOT NULL DEFAULT 0, telemetry TEXT NOT NULL DEFAULT '{}', refresh_s INTEGER NOT NULL DEFAULT 0,"
               " next_expected_at REAL NOT NULL DEFAULT 0, pair_until REAL NOT NULL DEFAULT 0, mains INTEGER NOT NULL DEFAULT 0,"
               " min_interval_s INTEGER NOT NULL DEFAULT 300, max_interval_s INTEGER NOT NULL DEFAULT 3600,"
               " heartbeat_s INTEGER NOT NULL DEFAULT 3600, footer INTEGER NOT NULL DEFAULT 1, drawn_filename TEXT NOT NULL DEFAULT '',"
               " drawn_at REAL NOT NULL DEFAULT 0, drawn_key TEXT NOT NULL DEFAULT '')")
    db.execute("ALTER TABLE displays DROP COLUMN kind")
    for column in ("eink_ready", "eink_dither", "eink_refresh"):
        db.execute(f"ALTER TABLE content_items DROP COLUMN {column}")
    db.execute("CREATE TABLE o2 (id INTEGER PRIMARY KEY AUTOINCREMENT, display_id INTEGER NOT NULL, url TEXT NOT NULL, expires_at REAL NOT NULL)")
    db.execute("DROP TABLE overrides")
    db.execute("ALTER TABLE o2 RENAME TO overrides")
    db.commit()
    db.close()
    upgraded = make_server()
    assert [d["kind"] for d in upgraded.client.get("/api/v1/displays").json["displays"]] == ["screen", "screen"]
    assert upgraded.client.post("/api/v1/displays/pi:Left/override", json={"content": "News"}).status_code == 200
    device = Device(upgraded)
    device.adopt()
    assert device.wake().status_code == 200
    with sqlite3.connect(str(upgraded.data_dir / "vitalsigns.db")) as db:
        assert db.execute("SELECT eink_ready, eink_dither, eink_refresh FROM content_items").fetchone() == (0, "", 0)


# --- a frame that covers the edge of the panel ------------------------------------------------

def test_pages_are_drawn_inside_the_frame(panel):
    panel.show("News")
    panel.server.post(f"/displays/{panel.id}/eink", name="panel", footer="on", inset_top="7", inset_right="15",
                      inset_bottom="16", inset_left="14")
    assert panel.display()["eink"]["insets"] == {"top": 7, "right": 15, "bottom": 16, "left": 14}
    panel.browser.colour = (255, 0, 0)
    panel.render()
    call = panel.browser.calls[0]
    assert (call["width"], call["height"]) == (800 - 14 - 15, 480 - 7 - 16 - eink.FOOTER_PX)
    picture = panel.picture(panel.row(panel.server.ids()["News"])["filename"]).convert("RGB")
    assert picture.size == (800, 480), "the picture is still the whole panel"
    red, white = (255, 0, 0), (255, 255, 255)
    assert [picture.getpixel(xy) for xy in ((13, 100), (14, 100), (784, 100), (785, 100))] == [white, red, red, white]
    assert [picture.getpixel((400, y)) for y in (6, 7, 435, 436)] == [white, red, red, (0, 0, 0)], "then the footer's rule"
    assert {picture.getpixel((x, y)) for x in range(0, 800, 7) for y in range(464, 480)} == {white}, "nothing under the frame"
    assert {picture.getpixel((x, y)) for x in (0, 13, 785, 799) for y in range(0, 480, 5)} == {white}


def test_changing_the_frame_redraws(panel):
    panel.show("News")
    panel.render()
    first = panel.row(panel.server.ids()["News"])["filename"]
    panel.server.post(f"/displays/{panel.id}/eink", name="panel", footer="on", inset_bottom="10")
    assert panel.render() == 1 and panel.row(panel.server.ids()["News"])["filename"] != first
    page = panel.server.post(f"/displays/{panel.id}/eink", name="panel", inset_top="500")
    assert "between 0 and 120" in page.get_data(as_text=True)


def test_built_in_pictures_keep_clear_of_the_frame(panel):
    panel.server.post(f"/displays/{panel.id}/eink", name="panel", inset_left="100", inset_right="100", inset_top="100", inset_bottom="100")
    picture = panel.picture(panel.wake()["filename"]).convert("RGB")
    edge = {picture.getpixel((x, y)) for x in list(range(0, 100, 3)) + list(range(700, 800, 3)) for y in range(0, 480, 3)}
    assert edge == {(255, 255, 255)}
