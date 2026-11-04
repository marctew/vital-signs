"""/healthz, which scripts/update.sh and the design updater wait on."""
import sqlite3

from server import auth


def test_healthz_answers_without_a_login(server):
    auth.set_password(server.data_dir, "correct horse")
    anon = server.app.test_client()
    r = anon.get("/healthz")
    assert r.status_code == 200
    assert r.json == {"ok": True, "busy": []}
    assert anon.get("/").status_code == 302          # the rest still needs the login


def test_healthz_fails_when_the_database_does_not_answer(server, monkeypatch):
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")
    monkeypatch.setattr("server.health.sqlite3.connect", broken)
    r = server.client.get("/healthz")
    assert r.status_code == 503
    assert r.json["ok"] is False and "database" in r.json["error"]
