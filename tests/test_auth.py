"""The optional login for the admin UI and the control API token."""
import pytest

from conftest import AGENT
from server import auth


@pytest.fixture(autouse=True)
def no_login_delay(monkeypatch):
    monkeypatch.setattr(auth.time, "sleep", lambda seconds: None)


def test_everything_is_open_without_a_password(server):
    assert server.client.get("/").status_code == 200
    assert server.client.get("/api/v1/displays").status_code == 200
    assert "Sign out" not in server.client.get("/").text


def test_password_protects_the_admin_ui_but_not_what_screens_use(server):
    server.poll()
    auth.set_password(server.data_dir, "correct horse")
    anon = server.app.test_client()
    r = anon.get("/playlists?x=1")
    assert r.status_code == 302 and r.headers["Location"] == "/login?next=/playlists?x%3D1"
    for path in ("/displays/1", "/screenshots/1.jpg", "/previews/1.jpg"):
        assert anon.get(path).status_code == 302, path
    assert anon.post("/update").status_code == 302
    assert anon.get("/api/v1/displays").status_code == 401

    assert anon.get("/pages/clock/").status_code == 200
    assert anon.get("/static/admin.css").status_code == 200
    assert anon.get("/api/data/nope").status_code == 404
    assert anon.get("/api/module/1/config").status_code == 404
    assert anon.get("/split/1/").status_code == 404
    assert anon.post("/api/agent/poll", headers=AGENT, json={"hostname": "pi"}).status_code == 200


def test_login_logout_and_safe_redirects(server):
    auth.set_password(server.data_dir, "correct horse")
    anon = server.app.test_client()
    assert "Wrong password" in anon.post("/login", data={"password": "nope"}).text
    r = anon.post("/login", data={"password": "correct horse", "next": "/playlists"})
    assert r.headers["Location"] == "/playlists"
    assert "Sign out" in anon.get("/").text and anon.get("/api/v1/displays").status_code == 200
    assert anon.post("/login", data={"password": "correct horse", "next": "//evil.example"}).headers["Location"] == "/"
    anon.post("/logout")
    assert anon.get("/").status_code == 302


def test_control_token(make_server):
    server = make_server(control_token="ctl")
    assert server.client.get("/api/v1/displays").status_code == 401
    assert server.client.get("/api/v1/displays", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert server.client.get("/api/v1/displays", headers={"Authorization": "Bearer ctl"}).status_code == 200
    assert server.client.get("/").status_code == 200, "the token does not lock the UI"


def test_sessions_survive_a_restart(make_server):
    first = make_server()
    auth.set_password(first.data_dir, "correct horse")
    first.client.post("/login", data={"password": "correct horse"})
    cookie = first.client.get_cookie("session").value
    second = make_server()
    second.client.set_cookie("session", cookie)
    assert second.client.get("/").status_code == 200
