"""The API a TRMNL e-ink device calls: setup, display, log and its pictures."""
import json

import pytest

from server import db as database
from server import eink

MAC = "B0:A6:04:00:18:0C"
HEADERS = {"ID": MAC, "Model": "og_4clr", "FW-Version": "1.7.8", "Width": "800", "Height": "480", "RSSI": "-61"}


class Device:
    """A pretend TRMNL talking to a test server."""

    def __init__(self, server, mac=MAC):
        self.server, self.client = server, server.client
        self.headers = {**HEADERS, "ID": mac}
        self.key = None

    def setup(self):
        r = self.client.get("/api/setup", headers=self.headers)
        if r.status_code == 200:
            self.key = r.json["api_key"]
        return r

    def wake(self, **extra):
        headers = {**self.headers, "Access-Token": self.key or "", "Battery-Voltage": "3.92", **extra}
        return self.client.get("/api/display", headers=headers)

    def adopt(self):
        self.setup()
        r = self.client.post("/eink/devices/1/adopt")
        assert r.status_code == 302
        return int(r.headers["Location"].rsplit("/", 1)[1])

    def row(self):
        conn = database.connect(self.server.app.config["DB_PATH"])
        try:
            return dict(conn.execute("SELECT * FROM eink_devices WHERE mac = ?", (self.headers["ID"],)).fetchone())
        finally:
            conn.close()


@pytest.fixture
def device(server):
    return Device(server)


def test_a_new_device_gets_a_key_and_waits_to_be_adopted(device, server):
    r = device.setup()
    assert r.status_code == 200 and r.json["status"] == 200 and r.json["friendly_id"] == "00180C"
    assert len(r.json["api_key"]) >= 24
    assert device.setup().json["api_key"] == r.json["api_key"]      # asking again changes nothing

    reply = device.wake().json
    assert reply["status"] == 0 and reply["refresh_rate"] == eink.ADOPT_RATE
    assert "/api/eink/image/pending-1/" in reply["image_url"]
    assert not server.client.get("/api/v1/displays").json["displays"]       # not a display until adopted
    assert "New e-ink display 00180C" in server.client.get("/").get_data(as_text=True)


def test_adopting_makes_it_a_display(device, server):
    display_id = device.adopt()
    displays = server.client.get("/api/v1/displays").json["displays"]
    assert [(d["id"], d["kind"], d["ref"], d["width"], d["height"]) for d in displays] == [
        (display_id, "eink", "trmnl-00180c:panel", 800, 480)]
    assert "New e-ink display" not in server.client.get("/").get_data(as_text=True)
    assert server.client.get(f"/displays/{display_id}").status_code == 200


def test_the_key_is_not_handed_out_again_once_adopted(device, server):
    display_id = device.adopt()
    r = device.setup()
    assert r.status_code == 404 and r.json["api_key"] is None
    server.post(f"/displays/{display_id}/eink/pair")           # Re-pair opens it for a few minutes, once
    assert device.setup().status_code == 200
    assert device.setup().status_code == 404


def test_a_wrong_or_missing_key_is_refused(device, server):
    device.adopt()
    assert device.wake(**{"Access-Token": "nope"}).status_code == 401
    assert server.client.get("/api/display", headers=HEADERS).status_code == 401
    assert server.client.get("/api/display").status_code == 400            # no MAC at all
    assert server.client.post("/api/log", headers={**HEADERS, "Access-Token": "nope"}, json={"logs": []}).status_code == 401


def test_a_device_that_already_has_a_key_keeps_it(server):
    """One set up against another server, or forgotten here, arrives at /api/display with a key."""
    device = Device(server)
    device.key = "key-from-elsewhere"
    assert device.wake().json["refresh_rate"] == eink.ADOPT_RATE
    assert device.row()["api_key"] == "key-from-elsewhere" and device.row()["agent_id"] is None
    assert device.wake(**{"Access-Token": "another"}).status_code == 401


def test_a_wake_records_what_the_device_reports(device, server):
    display_id = device.adopt()
    device.wake(**{"Battery-Voltage": "3.55", "RSSI": "-80", "Update-Source": "timer", "FW-Version": "1.8.0"})
    d = server.client.get(f"/api/v1/displays/{display_id}").json
    assert d["online"] is True and d["agent_sha"] == "1.8.0"
    e = d["eink"]
    assert (e["battery_v"], e["battery_percent"], e["rssi"], e["wifi"]) == (3.55, 50, -80, "weak")
    assert e["wake_reason"] == "timer" and e["model_name"] == "TRMNL BWRY" and e["palette"] == "bwry"
    assert e["usb"] is None                                                   # gen 1 cannot tell


def test_with_no_playlist_it_says_so(device, server):
    device.adopt()
    reply = device.wake().json
    assert reply["refresh_rate"] == 300 and reply["update_firmware"] is False and reply["reset_firmware"] is False
    picture = server.client.get(reply["image_url"], headers={"ID": MAC, "Access-Token": device.key})
    assert picture.status_code == 200 and picture.data.startswith(b"\x89PNG")
    assert device.wake().json["filename"] == reply["filename"]                # the same picture keeps its name


def test_pictures_need_the_devices_own_key(device, server):
    device.adopt()
    url = device.wake().json["image_url"]
    assert server.client.get(url).status_code == 401
    assert server.client.get(url, headers={"ID": MAC, "Access-Token": "nope"}).status_code == 401
    other = Device(server, "AA:BB:CC:DD:EE:FF")
    other.setup()
    assert server.client.get(url, headers={"ID": other.headers["ID"], "Access-Token": other.key}).status_code == 404
    assert server.client.get(url.replace("/1/", "/../"), headers={"ID": MAC, "Access-Token": device.key}).status_code == 404


def test_the_image_address_follows_the_reverse_proxy(device, server):
    device.adopt()
    reply = device.wake(**{"X-Forwarded-Proto": "https", "Host": "signs.example.org"}).json
    assert reply["image_url"].startswith("https://signs.example.org/api/eink/image/1/")


def test_logs_are_kept_and_shown(device, server):
    display_id = device.adopt()
    r = server.client.post("/api/log", headers={"ID": MAC, "Access-Token": device.key},
                           json={"logs": [{"message": "wifi connect failed", "id": 3}]})
    assert r.status_code == 204
    assert "wifi connect failed" in server.client.get(f"/displays/{display_id}").get_data(as_text=True)


def test_forgetting_a_pending_device(device, server):
    device.setup()
    server.post("/eink/devices/1/forget")
    assert "New e-ink display" not in server.client.get("/").get_data(as_text=True)


def test_the_device_api_needs_no_admin_login(make_server):
    from server import auth
    server = make_server()
    auth.set_password(server.data_dir, "correct horse")
    assert server.client.get("/").status_code == 302
    device = Device(server)
    assert device.setup().status_code == 200
    assert device.wake().status_code == 200


def test_an_eink_device_is_not_asked_to_update_itself(device, server):
    device.adopt()
    server.poll()
    server.post("/update")
    conn = database.connect(server.app.config["DB_PATH"])
    hosts = [r[0] for r in conn.execute("SELECT a.hostname FROM commands c JOIN agents a ON a.id = c.agent_id")]
    conn.close()
    assert hosts == ["pi"]


def test_settings_are_saved_and_checked(device, server):
    display_id = device.adopt()
    server.post(f"/displays/{display_id}/eink", name="Kitchen", mains="on", min_minutes="2", max_minutes="30",
                heartbeat_minutes="120", footer="on")
    row = device.row()
    assert (row["mains"], row["min_interval_s"], row["max_interval_s"], row["heartbeat_s"], row["footer"]) == (1, 120, 1800, 7200, 1)
    assert server.client.get(f"/api/v1/displays/{display_id}").json["ref"] == "trmnl-00180c:Kitchen"
    page = server.post(f"/displays/{display_id}/eink", name="Kitchen", min_minutes="90", max_minutes="30", heartbeat_minutes="60")
    assert "cannot be shorter" in page.get_data(as_text=True)
    assert device.row()["min_interval_s"] == 120


def test_json_shape_of_the_display_reply(device, server):
    device.adopt()
    reply = device.wake().json
    assert set(reply) == {"status", "image_url", "filename", "refresh_rate", "update_firmware", "firmware_url",
                          "reset_firmware", "special_function"}
    assert json.dumps(reply)        # plain JSON types only
