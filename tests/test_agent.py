"""Agent logic that needs no browser or compositor: outputs, schedules, and the player's decisions."""
from datetime import datetime

import pytest

from agent import outputs, player
from agent.config import Config, Output
from agent.outputs import Geometry

WLR_RANDR_TEXT = '''HDMI-A-1 "LG Electronics LG ULTRAWIDE (HDMI-A-1)"
  Enabled: yes
  Modes:
    2560x1080 px, 59.978001 Hz (preferred, current)
    1920x1080 px, 60.000000 Hz
  Position: 0,0
  Transform: 90
  Scale: 1.000000
HDMI-A-2 "LG"
  Enabled: no
  Modes:
    2560x1080 px, 59.978001 Hz (preferred, current)
  Position: 1080,0
  Transform: 90
  Scale: 1.000000
'''


def test_wlr_randr_parsing():
    found = outputs._parse_text(WLR_RANDR_TEXT)
    assert found == {"HDMI-A-1": Geometry(0, 0, 1080, 2560, "90")}, "rotated size, disabled output skipped"
    assert found["HDMI-A-1"].orientation == "portrait"
    as_json = '[{"name":"DP-1","enabled":true,"modes":[{"width":1920,"height":1080,"current":true}],' \
              '"position":{"x":5,"y":0},"transform":"normal","scale":1.0}]'
    assert outputs._parse_json(as_json) == {"DP-1": Geometry(5, 0, 1920, 1080, "normal")}


def test_compositor_config_rendering():
    cfg = Config(server_url="http://x", token="t", hostname="h", outputs=[
        Output("HDMI-A-1", "left", 9222, rotation=90), Output("HDMI-A-2", "right side", 9223, rotation=0, x=1080)])
    kanshi = outputs.render_kanshi(cfg)
    assert "output HDMI-A-1 enable position 0,0 transform 90" in kanshi
    assert "output HDMI-A-2 enable position 1080,0 transform normal" in kanshi
    assert 'identifier="vitalsigns-right-side"' in outputs.render_labwc_rules(cfg)


WEEKDAYS = {"on": "07:00", "off": "19:00", "days": [0, 1, 2, 3, 4]}
FRIDAY_NIGHT = {"on": "22:00", "off": "06:00", "days": [4]}
MONDAY, FRIDAY, SATURDAY = (2026, 10, 5), (2026, 10, 9), (2026, 10, 10)


@pytest.mark.parametrize("periods, day, clock, expected", [
    (None, MONDAY, (3, 0), True),
    ([], MONDAY, (3, 0), True),
    ([WEEKDAYS], MONDAY, (6, 59), False),
    ([WEEKDAYS], MONDAY, (7, 0), True),
    ([WEEKDAYS], MONDAY, (18, 59), True),
    ([WEEKDAYS], MONDAY, (19, 0), False),
    ([WEEKDAYS], SATURDAY, (12, 0), False),
    ([FRIDAY_NIGHT], FRIDAY, (21, 59), False),
    ([FRIDAY_NIGHT], FRIDAY, (23, 0), True),
    ([FRIDAY_NIGHT], SATURDAY, (5, 59), True),
    ([FRIDAY_NIGHT], SATURDAY, (6, 0), False),
    ([FRIDAY_NIGHT], SATURDAY, (23, 0), False),
    ([WEEKDAYS, FRIDAY_NIGHT], FRIDAY, (20, 0), False),
    ([WEEKDAYS, FRIDAY_NIGHT], FRIDAY, (22, 30), True),
    (WEEKDAYS, MONDAY, (8, 0), True),        # a single dict: state cached by an older version
])
def test_scheduled_on(periods, day, clock, expected):
    assert outputs.scheduled_on(periods, datetime(*day, *clock)) is expected


@pytest.fixture
def display(monkeypatch):
    """A Player with no browser: screen power calls are recorded and the schedule is set by the test."""
    calls, schedule = [], {"on": True}
    monkeypatch.setattr(outputs, "set_power", lambda connector, on: calls.append(on) or True)
    monkeypatch.setattr(outputs, "scheduled_on", lambda periods, when: schedule["on"] if periods else True)
    cfg = Config(server_url="http://x", token="t", hostname="h", outputs=[])
    p = player.Player(cfg, Output("HDMI-A-1", "left", 9222), Geometry(0, 0, 1080, 2560), api=None)
    clock = [0]

    def tick(state=None):
        if state is not None:
            p.state = state
        clock[0] += 10
        p.next_power = 0
        p._tick_power(clock[0])
        return p.screen_on

    p.tick, p.calls, p.schedule = tick, calls, schedule
    return p


PERIOD = [{"on": "07:00", "off": "19:00", "days": [0]}]


def test_no_schedule_never_touches_the_screen(display):
    assert display.tick({"power": None}) is True and display.calls == []


def test_schedule_turns_the_screen_off_and_an_override_url_wakes_it(display):
    display.schedule["on"] = False
    assert display.tick({"power": PERIOD}) is False
    display.override_tab = object()
    assert display.tick() is True, "showing an override wakes the screen"
    display.override_tab = None
    assert display.tick() is False
    assert display.tick({"power": None}) is True, "removing the schedule turns it back on"


def test_manual_power_override_holds_until_the_schedule_changes(display):
    forced_off = {"power": PERIOD, "power_override": {"id": 1, "on": False}}
    assert display.tick({"power": PERIOD, "power_override": None}) is True
    assert display.tick(forced_off) is False
    display.override_tab = object()
    assert display.tick() is False, "forced off wins over an override URL"
    display.override_tab = None
    assert display._build_status()["power_override_done"] is None
    display.schedule["on"] = False
    assert display.tick() is False and display._build_status()["power_override_done"] == 1
    display.schedule["on"] = True
    assert display.tick() is True, "an ended override must not hold the screen off the next morning"


def test_manual_override_without_a_schedule_stays_until_cleared(display):
    assert display.tick({"power": None, "power_override": {"id": 3, "on": False}}) is False
    assert display.tick() is False
    assert display.tick({"power": None, "power_override": None}) is True


class FakeTab:
    def __init__(self, key, url):
        self.key, self.url, self.target_id, self.status, self.shadow = key, url, "tab:" + url, "ok", None


def test_scheduled_playlist_takes_over_and_hands_back(monkeypatch):
    active = {"on": False}
    monkeypatch.setattr(outputs, "period_on", lambda rule, when: active["on"])
    cfg = Config(server_url="http://x", token="t", hostname="h", outputs=[])
    p = player.Player(cfg, Output("HDMI-A-1", "left", 9222), Geometry(0, 0, 1080, 2560), api=None)
    opened = []
    p._open_tab = lambda key, url, zoom, css, refresh, now: opened.append(url) or FakeTab(key, url)
    p._close_tab = lambda tab: opened.append("close " + tab.url)
    p._activate = lambda target, now: setattr(p, "active", target)
    p.idle_target = "idle"
    item = lambda name: {"id": 1, "content_id": 7, "name": name, "url": f"https://{name}.example", "duration": 30}
    p.state = {"display_zoom": 1, "items": [item("day")], "override": None,
               "scheduled": [{"on": "06:30", "off": "08:30", "days": [0], "playlist": "News", "items": [item("news")]}]}
    p._reconcile(0)
    assert p.active == "tab:https://day.example" and p._showing_content() == 7
    p.dirty = False
    p._tick_scheduled_playlist(10)
    assert p.dirty is False
    active["on"] = True
    p._tick_scheduled_playlist(20)
    assert p.dirty is True, "the period started"
    p._reconcile(20)
    assert p.active == "tab:https://news.example"
    assert opened == ["https://day.example", "https://news.example", "close https://day.example"]
    active["on"] = False
    p._tick_scheduled_playlist(30)
    p._reconcile(30)
    assert p.active == "tab:https://day.example"


def test_preview_is_not_attributed_during_an_override(monkeypatch):
    cfg = Config(server_url="http://x", token="t", hostname="h", outputs=[])
    p = player.Player(cfg, Output("HDMI-A-1", "left", 9222), Geometry(0, 0, 1080, 2560), api=None)
    assert p._showing_content() is None, "nothing playing"
    tab = FakeTab("k", "https://a.example")
    p.tabs, p.sequence, p.index, p.active = {"k": tab}, [{"key": "k", "duration": 30, "item": {"content_id": 4}}], 0, tab.target_id
    assert p._showing_content() == 4
    p.override_tab = object()
    assert p._showing_content() is None
    p.override_tab = None
    tab.status = "loading"
    assert p._showing_content() is None, "a page that has not loaded is not a preview"
