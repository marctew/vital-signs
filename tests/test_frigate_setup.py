"""Frigate without the typing: the shared address, the camera picker and labels; and the memory guard."""
import json

from server import modules

FRIGATE = {
    "go2rtc": {"streams": {"LivingRoom": [], "Kitchen": []}},
    "cameras": {
        "LivingRoom": {"detect": {"width": 1280, "height": 720}, "live": {"stream_name": "LivingRoom"}},
        "Kitchen": {"detect": {"width": 1280, "height": 720}, "live": {"stream_name": "Kitchen"}},
        "BambuP1P": {"detect": {"width": 640, "height": 480}},
    },
}


def fake_frigate(monkeypatch):
    monkeypatch.setattr(modules, "_get", lambda url, headers=None: (json.dumps(FRIGATE).encode(), "application/json")
                        if url.endswith("/api/config") else (_ for _ in ()).throw(AssertionError(url)))


def test_labels_in_camera_lines():
    cams = modules.parse_frigate_config(FRIGATE, ["BambuP1P | P1P", "livingroom", "Nope | x"])
    assert [(c["name"], c["label"]) for c in cams] == [("BambuP1P", "P1P"), ("LivingRoom", "LivingRoom")]
    assert modules.parse_camera_lines([" Kitchen |  ", "A|B", ""]) == [("Kitchen", None), ("A", "B")]


def test_address_from_settings_and_the_picker(server, monkeypatch):
    fake_frigate(monkeypatch)
    page = server.client.get("/content?module=frigate").text
    assert "Could not fetch the camera list" in page, "no address anywhere yet"

    server.post("/settings", section="general", frigate_url="http://frigate.lan:5000/")
    assert "must start with http" in server.post("/settings", section="general", frigate_url="frigate.lan").text
    page = server.client.get("/content?module=frigate").text
    for name in ("LivingRoom", "Kitchen", "BambuP1P"):
        assert f'value="{name}"' in page, name
    row = page.split('value="BambuP1P"')[1].split("</div>")[0]
    assert "pictures only" in row, "a camera without a stream is marked"

    # pick two with the tick boxes, one with a label; no address typed into the module
    server.post("/content/module/frigate", name="Printers", opt_frigate="", opt_cameras="", opt_layout="grid",
                opt_cameras_pick=["BambuP1P", "Kitchen"], opt_cameras_label_BambuP1P="P1P", opt_mode="snapshots",
                opt_interval="1", opt_fit="cover", opt_names="on")
    item = server.ids()["Printers"]
    options = server.client.get(f"/api/module/{item}/config").json["options"]
    assert options["frigate"] == "http://frigate.lan:5000", "filled in from Settings for the page"
    assert options["cameras"] == ["BambuP1P | P1P", "Kitchen"]
    data = server.client.get(f"/api/module/{item}/data").json["cameras"]
    assert [(c["name"], c["label"], c["stream"]) for c in data] == [("BambuP1P", "P1P", None), ("Kitchen", "Kitchen", "Kitchen")]

    edit = server.client.get(f"/content/{item}").text
    assert 'value="BambuP1P" checked' in edit and 'value="P1P"' in edit and 'value="Kitchen" checked' in edit

    # typing the list wins over the ticks, and sets the order
    server.post(f"/content/{item}/module/frigate", name="Printers", opt_cameras="Kitchen | Cook\nBambuP1P",
                opt_cameras_pick=["LivingRoom"])
    assert server.client.get(f"/api/module/{item}/config").json["options"]["cameras"] == ["Kitchen | Cook", "BambuP1P"]


def test_columns_setting_is_offered_and_kept(server, monkeypatch):
    fake_frigate(monkeypatch)
    server.post("/content/module/frigate", name="Rooms", opt_cameras="LivingRoom\nKitchen", opt_columns="1")
    options = server.client.get(f"/api/module/{server.ids()['Rooms']}/config").json["options"]
    assert options["columns"] == "1"
    server.post("/content/module/frigate", name="Odd", opt_cameras="Kitchen", opt_columns="9")
    assert server.client.get(f"/api/module/{server.ids()['Odd']}/config").json["options"]["columns"] == "auto"


def test_a_module_can_still_have_its_own_address(server, monkeypatch):
    fake_frigate(monkeypatch)
    server.post("/content/module/frigate", name="Other", opt_frigate="http://other:5000", opt_cameras="Kitchen")
    item = server.ids()["Other"]
    assert server.client.get(f"/api/module/{item}/config").json["options"]["frigate"] == "http://other:5000"


def test_memory_guard_relaunches_only_over_the_limit(monkeypatch):
    from agent import player
    from agent.config import Config, Output
    from agent.outputs import Geometry

    class QuietCDP:
        closed = False

        def send(self, method, params=None, session=None, timeout=None):
            return {}

    cfg = Config(server_url="http://x", token="t", hostname="h", outputs=[], browser_memory_limit_mb=3000)
    p = player.Player(cfg, Output("HDMI-A-1", "left", 9222), Geometry(0, 0, 1080, 2560), api=None)
    p.cdp = QuietCDP()
    readings = iter([1200, 3500])
    monkeypatch.setattr(p.browser, "alive", lambda: True)
    monkeypatch.setattr(p.browser, "memory_mb", lambda: next(readings))
    p.next_health = p.next_memory = 0
    p._tick_health(100)
    assert p.memory_mb == 1200 and p._build_status()["browser_memory_mb"] == 1200
    p.next_health = p.next_memory = 0
    try:
        p._tick_health(200)
        assert False, "should relaunch"
    except player.OverMemoryLimit as e:
        assert "3500 MB" in str(e) and isinstance(e, player.RestartRequested)

    cfg.browser_memory_limit_mb = 0
    monkeypatch.setattr(p.browser, "memory_mb", lambda: 9000)
    p.next_health = p.next_memory = 0
    p._tick_health(300)         # a limit of 0 never relaunches


def test_memory_reading_is_none_off_linux_or_when_dead():
    from agent.browser import Browser
    from agent.config import Config, Output
    from agent.outputs import Geometry
    b = Browser(Config(server_url="http://x", token="t", hostname="h", outputs=[]), Output("HDMI-A-1", "l", 9222), Geometry(0, 0, 1, 1))
    assert b.memory_mb() is None
