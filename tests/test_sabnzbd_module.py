"""SABnzbd queue module: only chosen categories ever reach the screen with names."""
import json
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from server import modules

QUEUE = {"queue": {
    "status": "Downloading", "paused": False, "kbpersec": "40960.0", "noofslots": 3,
    "slots": [
        {"filename": "Some.Show.S02E05.1080p.WEB", "cat": "tv", "status": "Downloading", "percentage": "63",
         "mb": "1800.0", "mbleft": "666.0", "timeleft": "0:02:20"},
        {"filename": "Private.Thing.2024.XXX", "cat": "adult", "status": "Downloading", "percentage": "12",
         "mb": "4000.0", "mbleft": "3520.0", "timeleft": "0:10:00"},
        {"filename": "Ubuntu.24.04.desktop", "cat": "*", "status": "Queued", "percentage": "0",
         "mb": "5800.0", "mbleft": "5800.0", "timeleft": "1:02:03"},
    ]}}
HISTORY = {"history": {"slots": [
    {"name": "Last.Show.S03E08", "category": "tv", "status": "Extracting", "bytes": 1572864000, "completed": 0},
    {"name": "Another.Secret.One", "category": "adult", "status": "Completed", "bytes": 1048576000, "completed": 1763000000},
    {"name": "Old.Film.2023", "category": "movies", "status": "Failed", "bytes": 0, "completed": 1762990000},
]}}
CATS = {"categories": ["*", "tv", "movies", "adult"]}


class FakeSab:
    def __init__(self):
        self.calls = []
        self.key = "k3y"

    def urlopen(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        self.calls.append(url)
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert urlsplit(url).path == "/sab/api" and q["output"] == "json"
        if q["apikey"] != self.key:
            body = {"status": False, "error": "API Key Incorrect"}
        else:
            body = {"queue": QUEUE, "history": HISTORY, "get_cats": CATS}[q["mode"]]
        return Reply(json.dumps(body).encode())


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


@pytest.fixture
def sab(server, monkeypatch):
    fake = FakeSab()
    monkeypatch.setattr(modules.urllib.request, "urlopen", fake.urlopen)
    monkeypatch.setattr(modules, "_cache", {})
    server.fake = fake
    return server


def add_sab(server, name="Downloads", named=("tv",), **opts):
    form = {"opt_title": "Downloads", "opt_server": "http://sab.lan:8080/sab/", "opt_api_key": "k3y",
            "opt_others": "label", "opt_idle_mode": "text", "opt_idle": "Nothing downloading",
            "opt_named_pick": list(named)}
    form.update({f"opt_{k}": v for k, v in opts.items()})
    server.post("/content/module/sabnzbd", name=name, **form)
    return server.ids()[name]


def test_other_categories_reach_the_page_without_name_or_category(sab):
    item = add_sab(sab)
    raw = sab.client.get(f"/api/module/{item}/data").get_data(as_text=True)
    assert "Private" not in raw and "Secret" not in raw and "adult" not in raw and "Ubuntu" not in raw
    data = json.loads(raw)
    assert [(i["name"], i["category"], i["private"]) for i in data["items"]] == [
        ("Some Show S02E05 1080p WEB", "tv", False), ("Other download", None, True), ("Other download", None, True)]
    assert data["items"][1]["progress"] == 12 and data["items"][1]["left_mb"] == 3520.0, "progress still shows"
    assert data["status"] == "Downloading" and data["speed_bps"] == 40960 * 1024
    assert data["eta_s"] == 3723 and data["left_mb"] == 9986.0
    assert [(p["name"], p["status"]) for p in data["processing"]] == [("Last Show S03E08", "Extracting")]
    assert [(r["name"], r["status"]) for r in data["recent"]] == [("Other download", "Completed"), ("Other download", "Failed")]
    saved = next((sab.data_dir / "module-cache").glob(f"{item}-*.json")).read_text()
    assert "Private" not in saved and "adult" not in saved, "nothing private is written to the disk cache either"


def test_other_categories_can_be_left_out(sab):
    item = add_sab(sab, named=("tv", "*"), others="hide")
    data = sab.client.get(f"/api/module/{item}/data").json
    assert [(i["name"], i["category"]) for i in data["items"]] == [
        ("Some Show S02E05 1080p WEB", "tv"), ("Ubuntu 24 04 desktop", None)]
    assert data["left_mb"] == 6466.0 and data["recent"] == []


def test_nothing_named_by_default(sab):
    item = add_sab(sab, named=())
    data = sab.client.get(f"/api/module/{item}/data").json
    assert {i["name"] for i in data["items"] + data["processing"] + data["recent"]} == {"Other download"}


def test_the_page_never_gets_the_address_or_key(sab):
    item = add_sab(sab)
    options = sab.client.get(f"/api/module/{item}/config").json["options"]
    assert "api_key" not in options and "server" not in options and options["named"] == ["tv"]


def test_a_wrong_key_says_so(sab):
    sab.fake.key = "other"
    item = add_sab(sab)
    r = sab.client.get(f"/api/module/{item}/data")
    assert r.status_code == 400 and "API key" in r.json["error"]


def test_the_form_offers_sabnzbd_categories_as_ticks(sab):
    item = add_sab(sab, named=("tv", "gone"))
    page = sab.client.get(f"/content/{item}").text
    for cat in ("tv", "movies", "adult"):
        assert f'name="opt_named_pick" value="{cat}"' in page
    assert 'value="tv" checked' in page and 'value="adult" checked' not in page
    assert "Default (no category)" in page and "(not in SABnzbd now)" in page
    assert "k3y" not in page


def test_new_items_can_type_categories(server):
    page = server.client.get("/content?module=sabnzbd").text
    assert 'textarea name="opt_named"' in page
    server.post("/content/module/sabnzbd", name="Sab", opt_server="http://sab.lan:8080", opt_api_key="k",
                opt_named="TV\nmovies\ntv\n", opt_others="label", opt_idle_mode="text")
    with server.db() as db:
        cfg = json.loads(db.execute("SELECT config FROM content_items WHERE name = 'Sab'").fetchone()[0])
    assert cfg["named"] == ["tv", "movies"]


def test_unreachable_sabnzbd_falls_back_to_typing(sab, monkeypatch):
    item = add_sab(sab)

    def down(*a, **k):
        raise urllib.error.URLError("refused")
    monkeypatch.setattr(modules.urllib.request, "urlopen", down)
    assert 'textarea name="opt_named"' in sab.client.get(f"/content/{item}").text
