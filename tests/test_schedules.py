"""Schedules: screen power, the manual power override, and playlists by time."""
import json
import sqlite3

import pytest

from server import services

WEEKDAYS = ["0", "1", "2", "3", "4"]


@pytest.fixture
def scheduled(server):
    server.poll()
    server.post("/schedules", name="Mornings", on="06:30", off="08:30", days=WEEKDAYS)
    server.post("/schedules", name="Evenings", on="17:30", off="23:00", days=WEEKDAYS)
    server.ids_of = server.ids("power_schedules")
    return server


def power(server, connector="HDMI-A-1"):
    return server.poll()["displays"][connector]["power"]


def test_schedule_validation(server):
    assert "Name is required" in server.post("/schedules", name="", on="06:30", off="08:30", days=["0"]).text
    assert "Choose at least one day" in server.post("/schedules", name="x", on="06:30", off="08:30").text
    assert "times like 07:30" in server.post("/schedules", name="x", on="7", off="08:30", days=["0"]).text
    assert "must differ" in server.post("/schedules", name="x", on="07:00", off="07:00", days=["0"]).text
    server.post("/schedules", name="Mornings", on="06:30", off="08:30", days=["0"])
    assert "already exists" in server.post("/schedules", name="Mornings", on="01:00", off="02:00", days=["0"]).text


def test_assigning_schedules_sets_the_screen_power_periods(scheduled):
    assert power(scheduled) is None, "no schedules: always on"
    ids = scheduled.ids_of
    scheduled.post("/displays/1/power", schedule_ids=[str(ids["Mornings"]), str(ids["Evenings"])])
    assert power(scheduled) == [{"on": "06:30", "off": "08:30", "days": [0, 1, 2, 3, 4]},
                                {"on": "17:30", "off": "23:00", "days": [0, 1, 2, 3, 4]}]
    assert power(scheduled, "HDMI-A-2") is None
    page = scheduled.client.get("/displays/1").text
    assert page.count('class="tl-on"') >= 10, "timeline shows the periods"


def test_copy_schedules_and_delete(scheduled):
    ids = scheduled.ids_of
    scheduled.post("/displays/1/power", schedule_ids=[str(ids["Mornings"])])
    assert "Copied the screen schedules from Left to Right" in scheduled.post("/displays/2/power/copy", from_id="1").text
    assert power(scheduled, "HDMI-A-2") == power(scheduled)
    assert "Choose another display" in scheduled.post("/displays/2/power/copy", from_id="2").text
    scheduled.post(f"/schedules/{ids['Mornings']}/delete")
    assert power(scheduled) is None and power(scheduled, "HDMI-A-2") is None


def test_control_api_for_schedules(scheduled):
    names = [s["name"] for s in scheduled.client.get("/api/v1/schedules").json["schedules"]]
    assert names == ["Mornings", "Evenings"]
    r = scheduled.client.put("/api/v1/displays/pi:Left/power-schedules", json={"schedules": ["Evenings"]})
    assert [s["name"] for s in r.json["power_schedules"]] == ["Evenings"]
    assert scheduled.client.put("/api/v1/displays/pi:Left/power-schedules", json={"schedules": ["Nope"]}).status_code == 404
    assert scheduled.client.put("/api/v1/displays/pi:Left/power-schedules", json={"schedule_ids": []}).json["power_schedules"] == []


def test_manual_power_override_is_cleared_when_the_agent_reports_it_done(scheduled):
    state = lambda: scheduled.poll()["displays"]["HDMI-A-1"]["power_override"]
    assert state() is None
    assert "Resume schedule" in scheduled.post("/displays/1/power/override", state="off").text
    assert state() == {"id": 1, "on": False}
    assert scheduled.client.post("/api/v1/displays/1/power", json={"on": True}).json["power_override"] == "on"
    assert state() == {"id": 2, "on": True}
    done = lambda n: scheduled.poll(displays=[{"connector": "HDMI-A-1", "name": "Left", "power_override_done": n}])
    done(1)
    assert scheduled.client.get("/api/v1/displays/1").json["power_override"] == "on", "a stale id clears nothing"
    done(2)
    assert scheduled.client.get("/api/v1/displays/1").json["power_override"] is None
    assert scheduled.client.post("/api/v1/displays/1/power", json={"on": "yes"}).status_code == 400
    scheduled.client.post("/api/v1/displays/1/power", json={"on": False})
    assert scheduled.client.delete("/api/v1/displays/1/power").json["power_override"] is None


def test_power_button_matches_the_screen_state(scheduled):
    shown = lambda on: scheduled.poll(displays=[{"connector": "HDMI-A-1", "name": "Left", "screen_on": on}])
    shown(True)
    page = scheduled.client.get("/displays/1").text
    assert ">Screen off</button>" in page and ">Screen on</button>" not in page
    shown(False)
    page = scheduled.client.get("/displays/1").text
    assert ">Screen on</button>" in page and ">Screen off</button>" not in page


def test_playlists_by_time(scheduled):
    a, b = scheduled.add_content("A"), scheduled.add_content("B")
    scheduled.post("/playlists", name="Day")
    scheduled.post("/playlists", name="News")
    scheduled.post("/playlists/1/items", content_id=str(a), duration="30")
    scheduled.post("/playlists/2/items", content_id=str(b), duration="20")
    scheduled.client.put("/api/v1/displays/1/assignment", json={"playlist": "Day"})
    state = lambda: scheduled.poll()["displays"]["HDMI-A-1"]
    assert state()["scheduled"] == []
    page = scheduled.post("/displays/1/playlist-rules", schedule_id=str(scheduled.ids_of["Mornings"]), playlist_id="2").text
    assert "<strong>News</strong> during Mornings" in page
    rule = state()["scheduled"][0]
    assert (rule["playlist"], rule["on"], rule["off"]) == ("News", "06:30", "08:30")
    assert [i["name"] for i in rule["items"]] == ["B"] and [i["name"] for i in state()["items"]] == ["A"]
    r = scheduled.client.post("/api/v1/displays/pi:Left/playlist-rules", json={"schedule": "Evenings", "playlist": "Day"})
    assert [x["playlist"] for x in r.json["playlist_rules"]] == ["News", "Day"]
    scheduled.client.delete(f"/api/v1/displays/1/playlist-rules/{r.json['playlist_rules'][1]['id']}")
    scheduled.post(f"/schedules/{scheduled.ids_of['Mornings']}/delete")
    assert state()["scheduled"] == [], "deleting the schedule removes its rules"


def test_timeline_geometry():
    week = services.week_segments([{"name": "Late", "on": "22:00", "off": "03:00", "days": [4]},
                                   {"name": "Sun", "on": "23:00", "off": "01:00", "days": [6]}])
    assert week[4] == [{"left": 91.667, "width": 8.333, "label": "Late: 22:00 to 03:00"}]
    assert week[5] == [{"left": 0.0, "width": 12.5, "label": "Late: 22:00 to 03:00"}]
    assert week[0][0]["width"] == 4.167 and week[6][0]["left"] == 95.833, "Sunday night runs into Monday"
    assert services.week_segments([])[3] == [{"left": 0, "width": 100, "label": "Always on"}]


def test_old_single_schedule_is_migrated_to_a_named_one(make_server):
    first = make_server()
    first.poll()
    db = first.db()
    db.execute("DROP TABLE display_schedules")
    db.execute("DROP TABLE playlist_rules")
    db.execute("DROP TABLE power_schedules")
    db.execute("UPDATE displays SET power_schedule = ?", (json.dumps({"on": "07:00", "off": "19:00", "days": [0, 1]}),))
    db.commit()
    db.close()
    upgraded = make_server()
    schedules = upgraded.client.get("/api/v1/schedules").json["schedules"]
    assert len(schedules) == 1 and schedules[0]["name"] == "07:00 to 19:00" and schedules[0]["displays"] == ["Left", "Right"]
    assert power(upgraded) == [{"on": "07:00", "off": "19:00", "days": [0, 1]}]
    assert sqlite3.connect(str(upgraded.data_dir / "vitalsigns.db")).execute(
        "SELECT COUNT(*) FROM displays WHERE power_schedule != ''").fetchone()[0] == 0
