"""Pi health on the dashboard, restart and reboot commands, and fades between items."""
from agent import health, player
from agent.cdp import CDPDead, CDPError
from agent.config import Config, Output
from agent.outputs import Geometry
from server import services

READINGS = {"temperature_c": 52.3, "memory_used_percent": 31, "memory_total_mb": 8062, "disk_used_percent": 18,
            "load": 0.4, "uptime_s": 93784, "network": "wifi", "interface": "wlan0", "wifi_signal_dbm": -55,
            "under_voltage": False, "throttled": False, "under_voltage_seen": False, "throttled_seen": False}


def facts(readings):
    return {f["label"]: (f["value"], f["warn"]) for f in services.health_facts(readings)}


def test_health_is_stored_and_shown(server):
    server.poll(health=READINGS)
    agent = server.client.get("/api/v1/agents").json["agents"][0]
    assert agent["hostname"] == "pi" and agent["online"] and agent["displays"] == ["Left", "Right"]
    assert agent["health"]["temperature_c"] == 52.3
    page = server.client.get("/").text
    for text in ("Devices", "52 °C", "31% of 8 GB used", "Wi-Fi, -55 dBm", "1 d 2 h", "Restart browsers", "Reboot"):
        assert text in page, text
    server.poll()       # a poll without readings (an older agent) keeps the last ones
    assert server.client.get("/api/v1/agents").json["agents"][0]["health"]["load"] == 0.4


def test_health_warnings():
    assert facts(READINGS)["Power"] == ("OK", False)
    assert facts({**READINGS, "temperature_c": 83})["Temperature"] == ("83 °C (running hot)", True)
    assert facts({**READINGS, "under_voltage": True})["Power"][1] is True
    assert facts({**READINGS, "under_voltage_seen": True})["Power"] == ("under-voltage or slow-down since boot", True)
    assert facts({**READINGS, "wifi_signal_dbm": -82})["Network"] == ("Wi-Fi, -82 dBm (weak)", True)
    assert facts({"network": "ethernet"}) == {"Network": ("Ethernet", False)}
    assert services.health_facts({}) == []


def test_health_readings_never_fail_on_a_machine_without_them():
    readings = health.read()
    assert isinstance(readings, dict)       # on a development PC most readings are simply missing


def test_restart_and_reboot_commands(server):
    server.poll()
    assert "Restarting the browsers on pi" in server.post("/agents/1/command/restart_browser").text
    assert server.client.post("/api/v1/agents/pi/reboot").status_code == 200
    assert [(c["type"], c["connector"]) for c in server.poll()["commands"]] == [("restart_browser", None), ("reboot", None)]
    assert server.client.post("/agents/1/command/update").status_code == 404
    assert server.client.post("/api/v1/agents/nope/reboot").status_code == 404


def test_transition_setting(server):
    assert server.poll()["displays"]["HDMI-A-1"]["transition"] == "fade", "fade by default"
    server.post("/displays/1/transition", transition="slow")
    assert server.poll()["displays"]["HDMI-A-1"]["transition"] == "slow"
    assert "Unknown transition" in server.post("/displays/1/transition", transition="spin").text
    assert 'value="slow" selected' in server.client.get("/displays/1").text


class FakeCDP:
    closed = False

    def __init__(self, fail_evaluate=False):
        self.calls, self.fail_evaluate = [], fail_evaluate

    def send(self, method, params=None, session=None, timeout=None):
        if method == "Runtime.evaluate":
            if self.fail_evaluate:
                raise CDPError("page cannot run scripts")
            opacity, ms = params["expression"].rsplit("})(", 1)[1].rstrip(");").split(", ")
            self.calls.append((session, "fade", int(opacity), int(ms)))
        else:
            self.calls.append((method, params["targetId"]))
        return {}


def make_player(transition, cdp):
    cfg = Config(server_url="http://x", token="t", hostname="h", outputs=[])
    p = player.Player(cfg, Output("HDMI-A-1", "left", 9222), Geometry(0, 0, 1080, 2560), api=None)
    p.cdp, p.active = cdp, "old"
    p.sessions = {"old": "s-old", "new": "s-new"}
    p.state = {"transition": transition}
    p._halt.wait = lambda seconds: cdp.calls.append(("wait", seconds))
    return p


def test_fade_goes_to_black_switches_then_fades_in():
    cdp = FakeCDP()
    p = make_player("fade", cdp)
    p._activate("new", 0)
    assert cdp.calls == [
        ("s-old", "fade", 1, 400),      # old page fades to black
        ("s-new", "fade", 1, 0),        # new page is black before it is shown
        ("wait", 0.4),
        ("Target.activateTarget", "new"),
        ("s-new", "fade", 0, 400),      # new page fades in
        ("s-old", "fade", 0, 0),        # old page is left clean
    ]
    assert p.active == "new"


def test_no_fade_when_off_for_a_refresh_or_with_the_screen_off():
    for setup in ("none", "refresh", "screen off"):
        cdp = FakeCDP()
        p = make_player("none" if setup == "none" else "fade", cdp)
        p.screen_on = setup != "screen off"
        p._activate("new", 0, fade=setup != "refresh")
        assert cdp.calls == [("Target.activateTarget", "new")], setup


def test_a_page_that_cannot_fade_still_switches():
    cdp = FakeCDP(fail_evaluate=True)
    p = make_player("fade", cdp)
    p._activate("new", 0)
    assert ("Target.activateTarget", "new") in cdp.calls and p.active == "new"


class StuckPageCDP(FakeCDP):
    """Chromium answers, but one page never hands over a screenshot."""

    def send(self, method, params=None, session=None, timeout=None):
        if method == "Page.captureScreenshot":
            self.calls.append(("screenshot", session))
            raise CDPDead("Page.captureScreenshot timed out")
        if method == "Browser.getVersion":
            return {"product": "Chromium"}
        return super().send(method, params, session, timeout)


def test_a_page_that_gives_no_screenshot_does_not_restart_the_browser():
    cdp = StuckPageCDP()
    p = make_player("none", cdp)
    p.next_shot = 0
    p._tick_screenshot(100)                 # must not raise: raising here used to relaunch Chromium
    assert cdp.calls == [("screenshot", "s-old")]
    p.next_shot = 0
    p._tick_screenshot(200)
    assert len(cdp.calls) == 1, "the page is left alone for a while instead of being asked every time"
    p.active = "new"
    p.next_shot = 0
    p._tick_screenshot(300)
    assert cdp.calls[-1] == ("screenshot", "s-new"), "other pages are still captured"
    p.next_shot = 0
    p.active = "old"
    p._tick_screenshot(100 + player.SHOT_BACKOFF + 1)
    assert cdp.calls[-1] == ("screenshot", "s-old"), "and it is tried again later"


def test_browser_responds_tells_a_stuck_page_from_a_dead_browser():
    class Alive:
        def alive(self):
            return True

    p = make_player("none", StuckPageCDP())
    p.browser = Alive()
    assert p._browser_responds() is True

    class Silent(FakeCDP):
        def send(self, method, params=None, session=None, timeout=None):
            raise CDPDead(method + " timed out")

    p.cdp = Silent()
    assert p._browser_responds() is False
    p.cdp = StuckPageCDP()
    p.cdp.closed = True
    assert p._browser_responds() is False
