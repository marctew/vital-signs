"""The Flights overhead module: normalising, filtering, lookups and the data endpoint."""
import json
import urllib.error
from pathlib import Path

import pytest

from server import modules

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "adsblol_point.json").read_text())
CENTRE = (52.134, -0.990)
CFG = {"location": "52.134, -0.990", "range_km": 50, "min_alt": 0, "max_alt": 0, "hide_ground": True,
       "route": True, "photo": True, "trails": True}


@pytest.fixture(autouse=True)
def fresh_caches(monkeypatch):
    for name in ("_routes", "_photos", "_trails", "_lookup_down", "_cache"):
        monkeypatch.setattr(modules, name, {})


class Upstream:
    """Stands in for the network: aircraft from the fixture, routes and photos counted."""

    def __init__(self, monkeypatch, aircraft=None, routes=None, photos=None, fail=()):
        self.aircraft = FIXTURE if aircraft is None else aircraft
        self.routes = routes if routes is not None else []
        self.photos = photos or {}
        self.fail = set(fail)
        self.calls = []
        monkeypatch.setattr(modules, "_flight_get", self.get)
        monkeypatch.setattr(modules, "_flight_post", self.post)
        monkeypatch.setattr(modules, "_geocode", self.geocode)

    def geocode(self, location):
        self.calls.append(("geocode", location))
        return 52.1, -1.0, "Towcester"

    def get(self, url):
        self.calls.append(("get", url))
        for name in self.fail:
            if name in url:
                raise urllib.error.URLError("down")
        if "/v2/point/" in url:
            return json.dumps(self.aircraft).encode(), "application/json"
        hex_ = url.rsplit("/", 1)[-1]
        found = self.photos.get(hex_)
        photos = [{"thumbnail_large": {"src": f"https://t.plnspttrs.net/{hex_}.jpg"}, "link": "https://p/1",
                   "photographer": found}] if found else []
        return json.dumps({"photos": photos}).encode(), "application/json"

    def post(self, url, payload):
        self.calls.append(("post", [p["callsign"] for p in payload["planes"]]))
        if "routes" in self.fail:
            raise ValueError("empty reply")
        return self.routes

    def posted(self):
        return [c[1] for c in self.calls if c[0] == "post"]

    def count(self, kind):
        return sum(1 for c in self.calls if c[0] == kind)


def by_callsign(result):
    return {a["callsign"]: a for a in result["aircraft"]}


def test_normalising_the_live_format():
    aircraft = {a["callsign"]: a for a in (modules.normalise_aircraft(r, *CENTRE) for r in FIXTURE["ac"])}
    ein = aircraft["EIN172"]
    assert ein["type_name"] == "Airbus A-320neo" and ein["reg"] == "EI-NSG" and ein["icon"] == "jet"
    assert ein["airline"] == {"code": "EIN", "name": "Aer Lingus", "iata": "EI"}
    assert ein["alt"] == 21075 and ein["gs"] == 437 and ein["track"] == 140 and ein["vrate"] == -2304
    # adsb.lol gives the distance in nm and the direction itself: ours should agree
    raw = next(r for r in FIXTURE["ac"] if r["flight"].strip() == "EIN172")
    assert abs(ein["dist_km"] - raw["dst"] * 1.852) < 0.5 and abs(ein["bearing"] - raw["dir"]) <= 1
    assert aircraft["FJO68N"]["ground"] and aircraft["FJO68N"]["alt"] == 0
    assert aircraft["GTESJ"]["airline"] is None, "a registration is not an airline callsign"
    assert aircraft["GFRGP"]["icon"] == "heli" and aircraft["GTESJ"]["icon"] == "light"


def test_odd_aircraft():
    base = {"hex": "~abc123", "lat": 52.2, "lon": -1.0, "flight": "XYZ", "seen_pos": 1}
    assert modules.normalise_aircraft({**base, "seen_pos": 90}, *CENTRE) is None, "stale position"
    assert modules.normalise_aircraft({"hex": "abc", "seen_pos": 1}, *CENTRE) is None, "no position"
    odd = modules.normalise_aircraft(base, *CENTRE)
    assert odd["alt"] is None and odd["track"] is None and odd["type_name"] == "" and not odd["emergency"]
    assert modules.normalise_aircraft({**base, "squawk": "7700"}, *CENTRE)["emergency"]
    assert modules.normalise_aircraft({**base, "emergency": "general"}, *CENTRE)["emergency"]
    assert modules.normalise_aircraft({**base, "dbFlags": 1}, *CENTRE)["military"]


def test_filters():
    every = [modules.normalise_aircraft(r, *CENTRE) for r in FIXTURE["ac"]]
    near = modules.filter_aircraft(every, {**CFG, "range_km": 35})
    assert near and all(a["dist_km"] <= 35 for a in near)
    assert [a["dist_km"] for a in near] == sorted(a["dist_km"] for a in near), "nearest first"
    assert "FJO68N" not in {a["callsign"] for a in modules.filter_aircraft(every, CFG)}
    assert "FJO68N" in {a["callsign"] for a in modules.filter_aircraft(every, {**CFG, "hide_ground": False})}
    high = modules.filter_aircraft(every, {**CFG, "min_alt": 10000})
    assert {a["callsign"] for a in high} == {"EIN172", "EZY95GN", "NJE107P"}
    low = modules.filter_aircraft(every, {**CFG, "max_alt": 3000})
    assert low and all(a["alt"] <= 3000 for a in low)
    many = [dict(every[1], hex=f"{i:06x}") for i in range(400)]
    assert len(modules.filter_aircraft(many, CFG)) == modules.MAX_AIRCRAFT


def test_centre(monkeypatch):
    up = Upstream(monkeypatch)
    assert modules._centre({"location": " 52.134 , -0.990 "}) == (52.134, -0.99, "52.134, -0.990")
    assert up.count("geocode") == 0
    with pytest.raises(modules.ServiceError):
        modules._centre({"location": "95, 10"})
    with pytest.raises(modules.ServiceError):
        modules._centre({"location": ""})
    assert modules._centre({"location": "Towcester, UK"})[2] == "Towcester" and up.count("geocode") == 1


def test_fallback_source(monkeypatch):
    up = Upstream(monkeypatch, fail={"adsb.lol/v2"})
    result = modules.fetch_flights(CFG, 1)
    assert result["source"] == "airplanes.live" and result["aircraft"]
    up.fail = {"/v2/point/"}
    with pytest.raises(urllib.error.URLError):
        modules.fetch_flights(CFG, 1)


ROUTE = {"callsign": "EIN172", "plausible": 1, "_airport_codes_iata": "DUB-LHR", "_airports": [
    {"iata": "DUB", "icao": "EIDW", "name": "Dublin Airport", "location": "Dublin", "countryiso2": "IE"},
    {"iata": "LHR", "icao": "EGLL", "name": "London Heathrow Airport", "location": "London", "countryiso2": "GB"}]}


def test_routes(monkeypatch):
    implausible = {**ROUTE, "callsign": "EZY95GN", "plausible": 0}
    up = Upstream(monkeypatch, routes=[ROUTE, implausible])
    clock = [1_000_000.0]
    monkeypatch.setattr(modules.time, "time", lambda: clock[0])
    first = by_callsign(modules.fetch_flights(CFG, 1))
    assert first["EIN172"]["route"]["from"]["iata"] == "DUB" and first["EIN172"]["route"]["to"]["city"] == "London"
    assert first["EZY95GN"]["route"] is None, "an implausible route is not shown"
    asked = up.posted()[-1]
    assert "EIN172" in asked and "GTESJ" not in asked, "registrations are not looked up"
    modules.fetch_flights(CFG, 1)
    assert up.count("post") == 1, "everything was cached"
    clock[0] += modules.ROUTE_MISS_TTL + 1
    modules.fetch_flights(CFG, 1)
    assert up.count("post") == 2 and "EIN172" not in up.posted()[-1], "only misses are asked again"


def test_route_failure_does_not_fail_the_fetch(monkeypatch):
    up = Upstream(monkeypatch, fail={"routes"})
    result = modules.fetch_flights(CFG, 1)
    assert result["aircraft"] and all(a["route"] is None for a in result["aircraft"])
    modules.fetch_flights(CFG, 1)
    assert up.count("post") == 1, "backs off after a failure"
    assert not modules._routes, "a failure is not cached as 'no route'"


def test_photos(monkeypatch):
    up = Upstream(monkeypatch, photos={"4cad80": "A. Spotter"})
    first = modules.fetch_flights(CFG, 1)
    assert sum(1 for c in up.calls if "planespotters" in str(c)) == modules.PHOTOS_PER_FETCH
    for _ in range(4):
        result = modules.fetch_flights(CFG, 1)
    photos = [c for c in up.calls if "planespotters" in str(c)]
    assert len(photos) == len({c[1] for c in photos}), "misses are cached too"
    ein = by_callsign(result)["EIN172"]
    assert ein["photo"]["photographer"] == "A. Spotter" and ein["photo"]["src"].startswith("https://")
    assert first["aircraft"][0]["photo"] is None or first["aircraft"][0]["photo"]["src"]
    modules._photos.clear()
    up2 = Upstream(monkeypatch)
    modules.fetch_flights({**CFG, "photo": False, "route": False}, 1)
    assert up2.count("get") == 1 and up2.count("post") == 0, "only the aircraft list is fetched"


def test_trails(monkeypatch):
    up = Upstream(monkeypatch)
    clock = [1_000_000.0]
    monkeypatch.setattr(modules.time, "time", lambda: clock[0])
    modules.fetch_flights(CFG, 1)
    modules.fetch_flights(CFG, 1)
    assert len(by_callsign(modules.fetch_flights(CFG, 1))["EIN172"]["trail"]) == 1, "unmoved points are not repeated"
    moved = json.loads(json.dumps(FIXTURE))
    for raw in moved["ac"]:
        raw["lat"] += 0.01
    up.aircraft = moved
    assert len(by_callsign(modules.fetch_flights(CFG, 1))["EIN172"]["trail"]) == 2
    up.aircraft = {"ac": []}
    clock[0] += modules.TRAIL_FORGET + 1
    modules.fetch_flights(CFG, 1)
    assert modules._trails[1] == {}, "aircraft that have gone are forgotten"


def test_lookup_tables():
    airlines = modules._flight_table("airlines.json")
    assert airlines["RYR"][0] == "Ryanair" and airlines["SHT"][0] == "British Airways", "hand corrections win"
    assert "KNM" not in airlines, "defunct airlines are left out: their designators get reused"
    assert modules._flight_table("aircraft_types.json")["B738"][0] == "Boeing 737-800"


def test_module_item_end_to_end(server, monkeypatch):
    assert "flights" in modules.list_modules()
    server.post("/content/module/flights", name="Planes", opt_location="52.134, -0.990", opt_range_km="50",
                opt_min_alt="0", opt_max_alt="0", opt_hide_ground="on", opt_featured="cycle",
                opt_cycle_seconds="12", opt_photo="on", opt_route="on", opt_map_style="dark", opt_units="aviation")
    content_id = server.ids()["Planes"]
    assert server.client.get(f"/api/module/{content_id}/config").json["options"]["range_km"] == 50
    up = Upstream(monkeypatch)
    url = f"/api/module/{content_id}/data"
    good = server.client.get(url).json
    assert good["source"] == "adsb.lol" and good["centre"]["place"] == "52.134, -0.990" and good["aircraft"]
    assert all(a["trail"] == [] for a in good["aircraft"]), "tracks were left off"
    assert server.client.get("/pages/flights/").status_code == 200
    # both sources down: the last good result is served
    modules._cache.clear()
    up.fail = {"/v2/point/"}
    assert server.client.get(url).json == good
