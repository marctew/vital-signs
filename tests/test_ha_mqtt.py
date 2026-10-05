"""Home Assistant over MQTT: what is announced, what is published, and what commands do."""
import json

import pytest

from server import db as database
from server import ha_mqtt

HEALTH = {"temperature_c": 52.3, "memory_used_percent": 31, "disk_used_percent": 18, "load": 0.4,
          "wifi_signal_dbm": -55, "under_voltage": False, "throttled": False}


@pytest.fixture
def home(server):
    server.poll(health=HEALTH)
    server.add_content("News")
    server.post("/playlists", name="Main")
    server.post("/playlists", name="Evening")
    conn = database.connect(server.app.config["DB_PATH"])
    yield server, conn
    conn.close()


def configs(messages, component):
    return {t.split("/")[2]: json.loads(p) for t, p in messages.items() if t.startswith(f"homeassistant/{component}/")}


def test_every_pi_and_display_is_announced(home):
    server, conn = home
    messages = ha_mqtt.build_messages(conn, "homeassistant")
    switches, selects, sensors = configs(messages, "switch"), configs(messages, "select"), configs(messages, "sensor")
    binary, buttons = configs(messages, "binary_sensor"), configs(messages, "button")

    assert set(switches) == {"vitalsigns_display_1_screen", "vitalsigns_display_2_screen"}
    screen = switches["vitalsigns_display_1_screen"]
    assert screen["command_topic"] == "vitalsigns/display/1/set/screen" and screen["availability_topic"] == "vitalsigns/status"
    assert screen["device"] == {"identifiers": ["vitalsigns_display_1"], "name": "Vital Signs Left", "manufacturer": "Vital Signs",
                                "model": "Display 0x0", "via_device": "vitalsigns_agent_1"}
    assert selects["vitalsigns_display_1_playlist"]["options"] == ["None", "Evening", "Main"]
    assert {"vitalsigns_agent_1_temperature_c", "vitalsigns_agent_1_wifi_signal_dbm", "vitalsigns_display_2_showing"} <= set(sensors)
    assert sensors["vitalsigns_agent_1_temperature_c"]["device_class"] == "temperature"
    assert {"vitalsigns_agent_1_online", "vitalsigns_agent_1_power", "vitalsigns_display_1_online", "vitalsigns_display_1_problem"} <= set(binary)
    assert {"vitalsigns_display_1_identify", "vitalsigns_display_1_next", "vitalsigns_agent_1_reboot"} <= set(buttons)
    ids = [json.loads(p)["unique_id"] for t, p in messages.items() if t.endswith("/config")]
    assert len(ids) == len(set(ids)), "every entity has its own id"


def test_states_reflect_the_server(home):
    server, conn = home
    server.client.put("/api/v1/displays/1/assignment", json={"playlist": "Main"})
    server.poll(health=HEALTH, displays=[
        {"connector": "HDMI-A-1", "name": "Left", "screen_on": False, "browser": "ok",
         "current": {"name": "News"}, "errors": [{"name": "Grafana", "error": "HTTP 502"}]},
        {"connector": "HDMI-A-2", "name": "Right", "browser": "down"}])
    messages = ha_mqtt.build_messages(conn, "homeassistant")
    assert json.loads(messages["vitalsigns/display/1/state"]) == {
        "online": "ON", "screen": "OFF", "playlist": "Main", "showing": "News", "problem": "ON", "errors": ["Grafana: HTTP 502"]}
    right = json.loads(messages["vitalsigns/display/2/state"])
    assert (right["online"], right["playlist"], right["showing"]) == ("OFF", "None", "browser restarting")
    agent = json.loads(messages["vitalsigns/agent/1/state"])
    assert agent == {"online": "ON", "temperature_c": 52.3, "memory_used_percent": 31, "disk_used_percent": 18,
                     "load": 0.4, "wifi_signal_dbm": -55, "power_problem": "OFF"}


def test_readings_a_pi_does_not_report_are_not_announced(server):
    server.poll(health={"disk_used_percent": 40})
    conn = database.connect(server.app.config["DB_PATH"])
    sensors = configs(ha_mqtt.build_messages(conn, "homeassistant"), "sensor")
    conn.close()
    assert "vitalsigns_agent_1_disk_used_percent" in sensors and "vitalsigns_agent_1_temperature_c" not in sensors


def test_commands_from_home_assistant(home):
    server, conn = home
    do = lambda topic, payload="": ha_mqtt.handle_command(conn, topic, payload)
    assert do("vitalsigns/display/1/set/screen", "OFF") == "Left: screen off"
    assert server.client.get("/api/v1/displays/1").json["power_override"] == "off"
    assert do("vitalsigns/display/1/set/playlist", "Evening") == "Left: playlist Evening"
    assert server.client.get("/api/v1/displays/1").json["playlist"]["name"] == "Evening"
    assert do("vitalsigns/display/1/set/playlist", "None") and server.client.get("/api/v1/displays/1").json["playlist"] is None
    assert do("vitalsigns/display/2/press/identify", "PRESS") == "Right: identify"
    assert do("vitalsigns/agent/1/press/reboot", "PRESS") == "pi: reboot"
    assert [(c["type"], c["connector"]) for c in server.poll()["commands"]] == [("identify", "HDMI-A-2"), ("reboot", None)]

    for topic, payload in (("vitalsigns/display/1/set/screen", "maybe"), ("vitalsigns/display/1/set/playlist", "Nope"),
                           ("vitalsigns/display/99/press/reload", ""), ("vitalsigns/display/1/press/update", ""),
                           ("vitalsigns/agent/1/press/identify", ""), ("other/topic", "ON"), ("vitalsigns/status", "online")):
        assert do(topic, payload) is None, topic
    assert len(server.poll()["commands"]) == 2, "nothing else was queued"


class FakeClient:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.sent.append((topic, payload, retain))


def test_bridge_publishes_changes_only_and_removes_forgotten_displays(home):
    server, conn = home
    bridge = ha_mqtt.Bridge(server.app.config["DB_PATH"])
    bridge.client, bridge.settings = FakeClient(), {"prefix": "homeassistant"}
    first = bridge.sync(conn)
    assert first > 20 and all(retain for _, _, retain in bridge.client.sent)
    assert bridge.sync(conn) == 0, "nothing changed, nothing sent"

    server.client.put("/api/v1/displays/1/assignment", json={"playlist": "Main"})
    bridge.client.sent.clear()
    assert bridge.sync(conn) == 1 and bridge.client.sent[0][0] == "vitalsigns/display/1/state"

    server.post("/playlists", name="Night")         # the playlist selectors gain an option
    bridge.client.sent.clear()
    bridge.sync(conn)
    assert sorted(t for t, _, _ in bridge.client.sent) == [
        "homeassistant/select/vitalsigns_display_1_playlist/config", "homeassistant/select/vitalsigns_display_2_playlist/config"]

    conn.execute("DELETE FROM displays WHERE id = 2")
    conn.commit()
    bridge.client.sent.clear()
    bridge.sync(conn)
    cleared = [t for t, payload, _ in bridge.client.sent if payload == ""]
    assert "homeassistant/switch/vitalsigns_display_2_screen/config" in cleared and len(cleared) == 9
    assert not any("display_1" in t for t in cleared)


def test_settings_page(server):
    page = server.client.get("/settings").text
    assert "Home Assistant" in page and "Not running" in page
    assert "Enter the broker" in server.post("/settings", enabled="on", host="").text
    assert "Port must be" in server.post("/settings", host="broker", port="99999").text
    server.post("/settings", enabled="on", host="192.168.4.121", port="1883", username="vs", password="secret", prefix="homeassistant")
    conn = database.connect(server.app.config["DB_PATH"])
    assert ha_mqtt.load_settings(conn) == {"enabled": True, "host": "192.168.4.121", "port": 1883, "username": "vs",
                                           "password": "secret", "prefix": "homeassistant"}
    page = server.client.get("/settings").text
    assert "secret" not in page and "Stored. Leave blank to keep it." in page
    server.post("/settings", enabled="on", host="192.168.4.121", port="1883", username="vs", password="")
    assert ha_mqtt.load_settings(conn)["password"] == "secret", "a blank password keeps the stored one"
    conn.close()
