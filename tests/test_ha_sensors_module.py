"""Home Assistant sensors module: pages of readings from chosen entities."""
import json
import urllib.error

import pytest

from server import modules


def st(entity_id, state, **attributes):
    return {"entity_id": entity_id, "state": state, "last_changed": "2026-11-14T09:00:00+00:00", "attributes": attributes}


STATES = [
    st("sensor.solar_power", "3120.5", unit_of_measurement="W", friendly_name="Solar power", device_class="power"),
    st("sensor.house_battery", "18", unit_of_measurement="%", friendly_name="House battery"),
    st("climate.lounge", "heat", friendly_name="Lounge", current_temperature=20.5, temperature=21),
    st("binary_sensor.back_door", "on", friendly_name="Back door", device_class="door"),
    st("sensor.next_bin", "2026-11-17T07:00:00+00:00", friendly_name="Next bin", device_class="timestamp"),
    st("sensor.washer", "rinse_cycle", friendly_name="Washer"),
    st("sensor.flaky", "unavailable", friendly_name="Flaky"),
    st("sensor.secret_thing", "42", friendly_name="Not asked for"),
    st("automation.lights", "on", friendly_name="Lights automation"),
]

PAGES = [
    {"title": "Energy", "tiles": [
        {"entity": "sensor.solar_power", "display": "gauge", "min": "0", "max": "5000", "warn": "", "bad": ""},
        {"entity": "sensor.house_battery", "display": "bar", "warn": "30", "bad": "15", "label": "Battery"},
    ]},
    {"title": "", "tiles": [
        {"entity": "climate.lounge", "attribute": "current_temperature", "unit": "°C", "decimals": "1"},
        {"entity": "binary_sensor.back_door"},
        {"entity": "sensor.next_bin"},
        {"entity": "sensor.washer"},
        {"entity": "sensor.flaky"},
        {"entity": "sensor.gone"},
    ]},
]


class Reply:
    def __init__(self, body):
        self.body = body
        self.headers = type("H", (), {"get_content_type": lambda s: "application/json"})()

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeHA:
    def __init__(self):
        self.template_allowed, self.token, self.calls, self.states = True, "good", [], STATES

    def urlopen(self, req, timeout=None):
        url, auth = req.full_url, req.get_header("Authorization")
        sent = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), url, sent))
        if auth != f"Bearer {self.token}":
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        if url.endswith("/api/template"):
            if not self.template_allowed:
                raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
            variables = sent["variables"]
            if "wanted" in variables:
                rows = [s for s in self.states if s["entity_id"] in variables["wanted"]]
            else:
                rows = [[s["entity_id"], s["attributes"].get("friendly_name"), s["attributes"].get("unit_of_measurement") or ""]
                        for s in self.states if s["entity_id"].split(".")[0] not in variables["skip"]]
            return Reply(json.dumps(rows).encode())
        if url.endswith("/api/states"):
            return Reply(json.dumps(self.states).encode())
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


@pytest.fixture
def ha(server, monkeypatch):
    fake = FakeHA()
    monkeypatch.setattr(modules.urllib.request, "urlopen", fake.urlopen)
    monkeypatch.setattr(modules, "_cache", {})
    server.post("/settings", section="ha_api", ha_url="http://ha.lan:8123", ha_token="good")
    server.fake = fake
    return server


def add_item(server, pages=PAGES, name="Sensors", **opts):
    form = {"opt_title": "", "opt_pages": json.dumps(pages), "opt_page_seconds": "10", "opt_dots": "on",
            "opt_ha_url": "", "opt_ha_token": ""}
    form.update({f"opt_{k}": v for k, v in opts.items()})
    r = server.post("/content/module/ha-sensors", name=name, **form)
    return server.ids().get(name), r


# --- the pages an item stores ------------------------------------------------------------

def test_pages_are_cleaned_when_saved():
    pages = modules.clean_ha_pages(json.dumps([
        {"title": " Energy ", "tiles": [
            {"entity": " Sensor.Solar_Power ", "display": "gauge", "max": "5000", "decimals": "9"},
            {"entity": "", "display": "value"},
            {"entity": "sensor.x", "display": "dial"},
        ]},
        {"title": "Empty", "tiles": [{"entity": ""}]},
    ]))
    assert pages == [{"title": "Energy", "tiles": [
        {"entity": "sensor.solar_power", "attribute": "", "label": "", "display": "gauge", "unit": "",
         "min": 0, "max": 5000.0, "decimals": 4, "warn": None, "bad": None},
        {"entity": "sensor.x", "attribute": "", "label": "", "display": "value", "unit": "",
         "min": None, "max": None, "decimals": None, "warn": None, "bad": None},
    ]}], "blank readings and empty pages drop out, gauges get a range, odd values are reined in"


@pytest.mark.parametrize("pages, message", [
    ([{"tiles": [{"entity": "kitchen temperature"}]}], "is not an entity id"),
    ([{"tiles": [{"entity": "sensor.a", "attribute": "x y"}]}], "is not an attribute name"),
    ([{"tiles": [{"entity": "sensor.a", "display": "bar", "min": "10", "max": "5"}]}], "maximum must be more"),
    ("not json", "could not be read"),
    ({"tiles": []}, "could not be read"),
])
def test_bad_pages_are_refused(pages, message):
    with pytest.raises(modules.ServiceError, match=message):
        modules.clean_ha_pages(pages if isinstance(pages, str) else json.dumps(pages))


def test_pages_and_readings_are_capped():
    many = [{"title": str(n), "tiles": [{"entity": f"sensor.s{i}"} for i in range(40)]} for n in range(30)]
    pages = modules.clean_ha_pages(many)
    assert len(pages) == modules.HA_PAGES_MAX and all(len(p["tiles"]) == modules.HA_TILES_MAX for p in pages)


# --- turning states into readings --------------------------------------------------------

def test_states_become_readings():
    pages = modules.parse_ha_pages(modules.clean_ha_pages(PAGES), STATES)
    assert [p["title"] for p in pages] == ["Energy", ""]
    solar, battery = pages[0]["tiles"]
    assert solar == {"display": "gauge", "min": 0, "max": 5000.0, "decimals": None, "warn": None, "bad": None,
                     "label": "Solar power", "unit": "W", "kind": "number", "value": 3120.5}
    assert (battery["label"], battery["value"], battery["unit"], battery["warn"], battery["bad"]) == ("Battery", 18.0, "%", 30.0, 15.0)
    lounge, door, bin_day, washer, flaky, gone = pages[1]["tiles"]
    assert (lounge["kind"], lounge["value"], lounge["unit"], lounge["decimals"]) == ("number", 20.5, "°C", 1), "an attribute"
    assert (door["kind"], door["on"], door["text"]) == ("state", True, "Open"), "on and off in the device class's words"
    assert (bin_day["kind"], bin_day["text"]) == ("time", "2026-11-17T07:00:00+00:00")
    assert (washer["kind"], washer["text"]) == ("text", "Rinse cycle")
    assert (flaky["kind"], flaky["text"]) == ("missing", "Unavailable")
    assert (gone["kind"], gone["text"], gone["label"]) == ("missing", "Not found", "sensor.gone")


def test_an_attribute_takes_no_unit_from_the_state():
    tile = modules.clean_ha_pages([{"tiles": [{"entity": "climate.lounge", "attribute": "temperature"}]}])[0]["tiles"][0]
    state = st("climate.lounge", "heat", unit_of_measurement="x", temperature=21)
    assert modules.ha_tile(tile, state)["unit"] == ""


# --- through the server ------------------------------------------------------------------

def test_the_page_gets_only_the_chosen_readings(ha):
    item, _ = add_item(ha)
    options = ha.client.get(f"/api/module/{item}/config").json["options"]
    assert "ha_token" not in options and "ha_url" not in options
    data = ha.client.get(f"/api/module/{item}/data").json
    assert [len(p["tiles"]) for p in data["pages"]] == [2, 6]
    assert "Not asked for" not in json.dumps(data) and "secret_thing" not in json.dumps(data)
    method, url, sent = ha.fake.calls[-1]
    assert (method, url) == ("POST", "http://ha.lan:8123/api/template")
    assert sent["variables"]["wanted"] == sorted({t["entity"] for p in PAGES for t in p["tiles"]})


def test_a_token_that_cannot_render_templates_reads_all_states(ha):
    ha.fake.template_allowed = False
    item, _ = add_item(ha)
    data = ha.client.get(f"/api/module/{item}/data").json
    assert data["pages"][0]["tiles"][0]["value"] == 3120.5
    assert "Not asked for" not in json.dumps(data)
    assert ha.fake.calls[-1][1] == "http://ha.lan:8123/api/states"


def test_a_refused_token_says_so(ha):
    ha.fake.token = "other"
    item, _ = add_item(ha)
    r = ha.client.get(f"/api/module/{item}/data")
    assert r.status_code == 400 and "did not accept the access token" in r.json["error"]


def test_without_home_assistant_set_up_the_page_says_where(server, monkeypatch):
    monkeypatch.setattr(modules, "_cache", {})
    item, _ = add_item(server)
    r = server.client.get(f"/api/module/{item}/data")
    assert r.status_code == 400 and "Settings page" in r.json["error"]


def test_an_item_needs_at_least_one_reading(ha):
    item, r = add_item(ha, pages=[{"title": "Empty", "tiles": [{"entity": ""}]}])
    assert item is None and "Pages is required" in r.text


def test_a_bad_entity_is_refused_with_a_reason(ha):
    item, r = add_item(ha, pages=[{"tiles": [{"entity": "kitchen"}]}])
    assert item is None and "kitchen is not an entity id" in r.text


def test_the_form_suggests_entities_and_keeps_the_pages(ha):
    page = ha.client.get("/content?module=ha-sensors").text
    assert '<datalist id="ha-entities">' in page and 'ha-pages.js' in page
    assert '<option value="sensor.solar_power" data-name="Solar power" data-unit="W">' in page
    assert "automation.lights" not in page, "nothing that is not a reading"
    item, _ = add_item(ha)
    with ha.db() as db:
        stored = json.loads(db.execute("SELECT config FROM content_items WHERE id = ?", (item,)).fetchone()[0])
    assert stored["pages"] == modules.clean_ha_pages(PAGES)
    assert '"entity": "sensor.solar_power"' in ha.client.get(f"/content/{item}").text, "the editor starts from what is stored"


def test_the_form_without_home_assistant_says_entities_can_be_typed(server):
    page = server.client.get("/content?module=ha-sensors").text
    assert "Could not get the entity list from Home Assistant" in page and '<datalist id="ha-entities"></datalist>' in page
