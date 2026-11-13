"""3D printers module: Bambu Lab printers read from Home Assistant."""
import json
import urllib.error

import pytest

from server import modules

H2S, P1S = "h2s_0938bc572400026", "p1s_01p09c4a1401920"


def st(entity_id, state, **attributes):
    return {"entity_id": entity_id, "state": state, "attributes": attributes}


def tray(prefix, unit, slot, name, colour, kind, active=False):
    empty = name == "Empty"
    return st(f"sensor.{prefix}_ams_{unit}_tray_{slot}", name, color=colour, type=kind, active=active, empty=empty)


# Trimmed from a real Home Assistant with the ha-bambulab integration: an H2S mid-print, a P1S finished.
STATES = [
    st("sensor.h2s_0938bc572400026_print_status", "running"),
    st("sensor.h2s_0938bc572400026_print_progress", "24", unit_of_measurement="%"),
    st("sensor.h2s_0938bc572400026_current_stage", "printing"),
    st("sensor.h2s_0938bc572400026_task_name", "FloorLamp_plate_9"),
    st("sensor.h2s_0938bc572400026_printer_name", "H2S"),
    st("sensor.h2s_0938bc572400026_remaining_time", "3.41666666666667", unit_of_measurement="h"),
    st("sensor.h2s_0938bc572400026_end_time", "2026-11-13T04:03:00+00:00"),
    st("sensor.h2s_0938bc572400026_current_layer", "3"),
    st("sensor.h2s_0938bc572400026_total_layer_count", "165"),
    st("sensor.h2s_0938bc572400026_nozzle_temperature", "245", unit_of_measurement="°C"),
    st("sensor.h2s_0938bc572400026_nozzle_target_temperature", "245", unit_of_measurement="°C"),
    st("sensor.h2s_0938bc572400026_bed_temperature", "61.6", unit_of_measurement="°C"),
    st("sensor.h2s_0938bc572400026_bed_target_temperature", "70", unit_of_measurement="°C"),
    st("sensor.h2s_0938bc572400026_chamber_temperature", "36", unit_of_measurement="°C"),
    st("sensor.h2s_0938bc572400026_chamber_target_temperature", "0", unit_of_measurement="°C"),
    st("sensor.h2s_0938bc572400026_print_weight", "104.39", unit_of_measurement="g"),
    st("binary_sensor.h2s_0938bc572400026_online", "on"),
    st("binary_sensor.h2s_0938bc572400026_print_error", "off"),
    st("binary_sensor.h2s_0938bc572400026_hms_errors", "off"),
    st("binary_sensor.h2s_0938bc572400026_enclosure_door", "off"),
    st("camera.h2s_0938bc572400026_camera", "streaming"),
    st("image.h2s_0938bc572400026_cover_image", "2026-11-13T00:00:48"),
    st("sensor.h2s_0938bc572400026_ams_1_humidity", "25", unit_of_measurement="%"),
    st("sensor.h2s_0938bc572400026_ams_2_humidity", "21", unit_of_measurement="%"),
    tray(H2S, 2, 1, "Overture PLA", "#0ACC38FF", "PLA"),
    tray(H2S, 1, 1, "Empty", "#00000000", "Empty"),
    tray(H2S, 2, 3, "Bambu PETG Translucent", "#00000000", "PETG", active=True),
    tray(H2S, 1, 2, "Bambu PLA Glow", "#FFF144FF", "PLA"),
    st("sensor.h2s_0938bc572400026_externalspool_external_spool", "Empty", color="#00000000", type="Empty",
       active=False, empty=True),
    st("sensor.p1s_01p09c4a1401920_print_status", "finish"),
    st("sensor.p1s_01p09c4a1401920_print_progress", "100", unit_of_measurement="%"),
    st("sensor.p1s_01p09c4a1401920_printer_name", "Home2"),
    st("sensor.p1s_01p09c4a1401920_remaining_time", "0", unit_of_measurement="min"),
    st("sensor.p1s_01p09c4a1401920_end_time", "unknown"),
    st("sensor.p1s_01p09c4a1401920_nozzle_temperature", "28", unit_of_measurement="°C"),
    st("binary_sensor.p1s_01p09c4a1401920_online", "on"),
    st("camera.p1s_01p09c4a1401920_camera", "unavailable"),
    tray(P1S, 1, 2, "Bambu PLA-CF", "#898989FF", "PLA-CF"),
    st("sensor.p1s_01p09c4a1401920_externalspool_external_spool", "Bambu TPU 95A", color="#161616FF", type="TPU",
       active=False, empty=False),
    # Not printers: a print_status with no print_progress, and an unrelated sensor.
    st("sensor.octo_print_status", "Operational"),
    st("sensor.kitchen_temperature", "21"),
]


def test_printer_lines_take_prefixes_or_whole_entity_ids():
    assert modules.parse_printer_lines([
        f"{P1S} | Workshop", f"sensor.{H2S}_print_status", "Bad Prefix!", f"{P1S} | Again", ""]) == [
        (P1S, "Workshop"), (H2S, "")]


def test_printers_are_found_and_summarised():
    printers = modules.parse_printers(STATES)
    assert [p["id"] for p in printers] == [H2S, P1S], "every printer, and only printers"
    h2s, p1s = printers
    assert h2s["name"] == "H2S" and h2s["model"] == "H2S" and h2s["status"] == "running" and h2s["active"]
    assert h2s["progress"] == 24 and h2s["task"] == "FloorLamp_plate_9"
    assert h2s["remaining_min"] == 205, "hours from Home Assistant become minutes"
    assert (h2s["layer"], h2s["layers"]) == (3, 165)
    assert h2s["temps"] == [{"name": "Nozzle", "now": 245, "target": 245}, {"name": "Bed", "now": 62, "target": 70},
                            {"name": "Chamber", "now": 36, "target": 0}]
    assert h2s["door_open"] is False and h2s["error"] is False and h2s["camera"] and h2s["cover"]
    ams1, ams2 = h2s["filament"]
    assert ams1["name"] == "AMS 1" and ams1["humidity"] == 25
    assert [t["slot"] for t in ams1["trays"]] == ["1", "2"], "trays in slot order"
    assert ams1["trays"][0]["empty"] and ams1["trays"][0]["name"] is None
    assert ams2["trays"][0]["colour"] == "#0acc38"
    active = ams2["trays"][1]
    assert active["active"] and active["colour"] is None, "a clear filament has no colour to show"
    assert len(h2s["filament"]) == 2, "an empty external spool is left out when there is an AMS"

    assert p1s["name"] == "Home2" and p1s["status"] == "finish" and not p1s["active"]
    assert p1s["end"] is None and p1s["remaining_min"] == 0 and p1s["layers"] is None
    assert p1s["camera"] is False, "an unavailable camera is not offered"
    assert p1s["filament"][0]["name"] == "AMS" and p1s["filament"][1]["name"] == "External"
    assert p1s["filament"][1]["trays"][0]["name"] == "Bambu TPU 95A"


def test_chosen_printers_keep_their_order_labels_and_report_missing_ones():
    printers = modules.parse_printers(STATES, [(P1S, "Workshop"), ("x1c_nope", ""), (H2S, "")])
    assert [(p["id"], p["name"]) for p in printers] == [(P1S, "Workshop"), ("x1c_nope", "x1c_nope"), (H2S, "H2S")]
    assert printers[1]["missing"]


def test_an_offline_printer_says_so():
    states = [s for s in STATES if s["entity_id"] != f"binary_sensor.{H2S}_online"]
    states.append(st(f"binary_sensor.{H2S}_online", "off"))
    h2s = modules.parse_printers(states)[0]
    assert h2s["status"] == "offline" and not h2s["active"]


# --- through the server ------------------------------------------------------------------

class FakeHA:
    """Stands in for Home Assistant's REST API."""

    def __init__(self, template_allowed=True, token="good"):
        self.template_allowed, self.token, self.calls, self.states = template_allowed, token, [], STATES

    def urlopen(self, req, timeout=None):
        url = req.full_url
        auth = req.get_header("Authorization")
        self.calls.append((req.get_method(), url, auth))
        if auth != f"Bearer {self.token}":
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        if url.endswith("/api/template"):
            if not self.template_allowed:
                raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
            sent = json.loads(req.data)
            assert "_print_status" in sent["template"]
            wanted = sent["variables"]["wanted"]
            body = json.dumps([s for s in self.states if not wanted or any(f".{w}_" in s["entity_id"] for w in wanted)])
            return Reply(body.encode(), "application/json")
        if url.endswith("/api/states"):
            return Reply(json.dumps(self.states).encode(), "application/json")
        if "/api/camera_proxy/" in url or "/api/image_proxy/" in url:
            return Reply(b"\xff\xd8jpeg", "image/jpeg")
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


class Reply:
    def __init__(self, body, content_type):
        self.body = body
        self.headers = type("H", (), {"get_content_type": lambda s: content_type})()

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def ha(server, monkeypatch):
    fake = FakeHA()
    monkeypatch.setattr(modules.urllib.request, "urlopen", fake.urlopen)
    monkeypatch.setattr(modules, "_cache", {})
    server.post("/settings", section="ha_api", ha_url="http://ha.lan:8123/", ha_token="good")
    server.fake = fake
    return server


def add_printer(server, name="Printers", **opts):
    form = {"opt_printers": "", "opt_show": "all", "opt_picture": "printing", "opt_interval": "5",
            "opt_fit": "cover", "opt_temps": "on", "opt_filament": "on", "opt_ha_url": "", "opt_ha_token": ""}
    form.update({f"opt_{k}": v for k, v in opts.items()})
    server.post("/content/module/printer", name=name, **form)
    return server.ids()[name]


def test_settings_store_the_address_and_keep_the_token(ha):
    page = ha.client.get("/settings").text
    assert "http://ha.lan:8123" in page and "Stored. Leave blank to keep it." in page and "good" not in page
    assert "must start with http" in ha.post("/settings", section="ha_api", ha_url="ha.lan").text
    ha.post("/settings", section="ha_api", ha_url="http://ha.lan:8123", ha_token="")
    with ha.db() as db:
        assert dict(db.execute("SELECT key, value FROM settings WHERE key LIKE 'ha_%'").fetchall()) == {
            "ha_url": "http://ha.lan:8123", "ha_token": "good"}
    ha.post("/settings", section="ha_api", ha_url="http://ha.lan:8123", ha_token="", ha_token_clear="on")
    with ha.db() as db:
        assert db.execute("SELECT value FROM settings WHERE key = 'ha_token'").fetchone()[0] == ""


def test_the_page_gets_printers_but_never_the_address_or_token(ha):
    item = add_printer(ha)
    options = ha.client.get(f"/api/module/{item}/config").json["options"]
    assert "ha_token" not in options and "ha_url" not in options
    data = ha.client.get(f"/api/module/{item}/data").json
    assert [p["id"] for p in data["printers"]] == [H2S, P1S]
    method, url, auth = ha.fake.calls[0]
    assert (method, url, auth) == ("POST", "http://ha.lan:8123/api/template", "Bearer good"), "one small template call"


def test_a_token_that_cannot_render_templates_reads_all_states(ha):
    ha.fake.template_allowed = False
    item = add_printer(ha, printers=f"{P1S} | Workshop")
    data = ha.client.get(f"/api/module/{item}/data").json
    assert [(p["id"], p["name"]) for p in data["printers"]] == [(P1S, "Workshop")]
    assert ha.fake.calls[-1][1] == "http://ha.lan:8123/api/states"


def test_a_refused_token_says_so(ha):
    ha.fake.token = "other"
    item = add_printer(ha)
    r = ha.client.get(f"/api/module/{item}/data")
    assert r.status_code == 400 and "did not accept the access token" in r.json["error"]


def test_without_home_assistant_set_up_the_page_says_where_to_do_it(server, monkeypatch):
    monkeypatch.setattr(modules, "_cache", {})
    item = add_printer(server)
    r = server.client.get(f"/api/module/{item}/data")
    assert r.status_code == 400 and "Settings page" in r.json["error"]


def test_no_printers_in_home_assistant(ha):
    ha.fake.states = [st("sensor.kitchen_temperature", "21")]
    item = add_printer(ha)
    r = ha.client.get(f"/api/module/{item}/data")
    assert r.status_code == 400 and "no Bambu Lab printers" in r.json["error"]


def test_pictures_come_through_the_server_with_the_token(ha):
    item = add_printer(ha)
    r = ha.client.get(f"/api/module/{item}/image?printer={H2S}&kind=camera&w=640")
    assert r.status_code == 200 and r.mimetype == "image/jpeg" and r.headers["Cache-Control"] == "no-store"
    assert ha.fake.calls[-1] == ("GET", f"http://ha.lan:8123/api/camera_proxy/camera.{H2S}_camera?width=640", "Bearer good")
    r = ha.client.get(f"/api/module/{item}/image?printer={P1S}&kind=cover")
    assert r.status_code == 200 and ha.fake.calls[-1][1] == f"http://ha.lan:8123/api/image_proxy/image.{P1S}_cover_image"
    for bad in ("printer=../x&kind=camera", f"printer={H2S}&kind=pick", "printer=sensor.x&kind=camera"):
        assert ha.client.get(f"/api/module/{item}/image?{bad}").status_code == 404, bad


def test_pictures_only_for_the_printers_chosen(ha):
    item = add_printer(ha, printers=P1S)
    assert ha.client.get(f"/api/module/{item}/image?printer={H2S}&kind=camera").status_code == 404
    assert ha.client.get(f"/api/module/{item}/image?printer={P1S}&kind=camera").status_code == 200
