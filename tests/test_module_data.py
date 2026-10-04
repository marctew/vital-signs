"""How module data behaves when its source is unreachable."""
import urllib.error

import pytest

from server import modules

WEATHER = {"place": "Towcester", "current": {"temperature": 12}, "daily": []}


@pytest.fixture
def weather(server, monkeypatch):
    server.post("/content/module/weather", name="Weather", opt_location="Towcester", opt_units="celsius",
                opt_wind="mph", opt_days="5")
    server.url = f"/api/module/{server.ids()['Weather']}/data"
    monkeypatch.setattr(modules, "_cache", {})
    return server


def failing(error):
    def fetch(cfg, content_id):
        raise error
    return fetch


def test_failure_with_nothing_saved_says_why(weather, monkeypatch):
    for error, words in ((urllib.error.URLError(TimeoutError("timed out")), "did not answer in time"),
                         (urllib.error.HTTPError("u", 429, "Too Many", {}, None), "error 429"),
                         (urllib.error.URLError(OSError(-2, "Name or service not known")), "DNS"),
                         (KeyError("current"), "not understood")):
        monkeypatch.setitem(modules.PROVIDERS, "weather", (failing(error), 900))
        r = weather.client.get(weather.url)
        assert r.status_code == 502 and words in r.json["error"], r.json


def test_last_good_data_survives_a_restart_and_an_outage(weather, monkeypatch):
    monkeypatch.setitem(modules.PROVIDERS, "weather", (lambda cfg, content_id: WEATHER, 900))
    assert weather.client.get(weather.url).json == WEATHER
    # the server restarts (memory cache gone) while the weather service is unreachable
    monkeypatch.setattr(modules, "_cache", {})
    monkeypatch.setitem(modules.PROVIDERS, "weather", (failing(urllib.error.URLError("down")), 900))
    r = weather.client.get(weather.url)
    assert r.status_code == 200 and r.json == WEATHER, "served from the copy saved on disk"


def test_saved_data_is_not_reused_after_the_settings_change(weather, monkeypatch):
    monkeypatch.setitem(modules.PROVIDERS, "weather", (lambda cfg, content_id: WEATHER, 900))
    weather.client.get(weather.url)
    content_id = weather.ids()["Weather"]
    weather.post(f"/content/{content_id}/module/weather", name="Weather", opt_location="Leeds", opt_units="celsius",
                 opt_wind="mph", opt_days="5")
    monkeypatch.setattr(modules, "_cache", {})
    monkeypatch.setitem(modules.PROVIDERS, "weather", (failing(urllib.error.URLError("down")), 900))
    assert weather.client.get(weather.url).status_code == 502, "Towcester's weather must not be shown for Leeds"
