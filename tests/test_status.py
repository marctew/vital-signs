"""The service status module's checks, against a real local web server."""
import http.server
import socket
import threading

import pytest

from server import modules


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code = {"/": 200, "/login": 401, "/broken": 500, "/moved": 302}.get(self.path, 404)
        self.send_response(code)
        if code == 302:
            self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def web():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]      # free again once the socket closes


def test_web_checks(web):
    up, ms, detail = modules.check_service(f"http://{web}/")
    assert (up, detail) == (True, "") and ms >= 0
    assert modules.check_service(f"http://{web}/moved")[0] is True, "redirects are followed"
    assert modules.check_service(f"http://{web}/broken")[::2] == (False, "error 500")
    assert modules.check_service(f"http://{web}/missing")[::2] == (False, "error 404")
    assert modules.check_service(f"http://{web}/login")[0] is True, "a login prompt means the service is there"
    assert modules.check_service(f"http://{web}/login", auth_ok=False)[::2] == (False, "error 401")


def test_port_checks_and_bad_addresses(web, closed_port):
    assert modules.check_service(f"tcp://{web}")[0] is True
    up, ms, detail = modules.check_service(f"tcp://127.0.0.1:{closed_port}", timeout=2)
    assert up is False and detail in ("refused", "no answer", "unreachable")
    assert modules.check_service(f"http://127.0.0.1:{closed_port}/", timeout=2)[0] is False
    for bad in ("ftp://example.com", "tcp://no-port", "just words", "", "-c", "ping://-f", "a;rm -rf /", "ping://host name"):
        assert modules.check_service(bad) == (False, None, "bad address"), bad


def test_ping():
    for target in ("127.0.0.1", "ping://127.0.0.1"):
        up, ms, detail = modules.check_service(target, timeout=2)
        assert (up, detail) == (True, "") and ms >= 0, target
    # 192.0.2.0/24 is reserved for documentation, so nothing answers there
    assert modules.check_service("192.0.2.1", timeout=1) == (False, None, "no answer")


def test_ping_reports_a_missing_ping_command(monkeypatch):
    monkeypatch.setattr(modules.shutil, "which", lambda name: None)
    assert modules.check_service("127.0.0.1") == (False, None, "ping is not installed on the server")


def test_service_lines_are_parsed():
    assert modules.parse_services(["Plex | http://10.0.0.2:32400/identity", "tcp://10.0.0.5:445", "  ", "NAS|tcp://nas:22", "Empty |"]) == [
        ("Plex", "http://10.0.0.2:32400/identity"), ("10.0.0.5", "tcp://10.0.0.5:445"), ("NAS", "tcp://nas:22")]
    assert modules.parse_services(["Router | 192.168.4.1", "192.168.4.2", "ping://pi.lan"]) == [
        ("Router", "192.168.4.1"), ("192.168.4.2", "192.168.4.2"), ("pi.lan", "ping://pi.lan")]


def test_status_data_tracks_when_a_service_changed(server, web, closed_port, monkeypatch):
    monkeypatch.setattr(modules, "_since", {})
    monkeypatch.setattr(modules, "_cache", {})
    server.post("/content/module/status", name="Status", opt_timeout="2", opt_auth_ok="on",
                opt_services=f"Web | http://{web}/\nDead | tcp://127.0.0.1:{closed_port}")
    item = server.ids()["Status"]
    assert "services" not in server.client.get(f"/api/module/{item}/config").json["options"], "addresses stay on the server"
    first = server.client.get(f"/api/module/{item}/data").json["services"]
    assert [(s["name"], s["up"]) for s in first] == [("Web", True), ("Dead", False)]
    monkeypatch.setattr(modules, "_cache", {})
    monkeypatch.setattr(modules.time, "time", lambda: first[1]["since"] + 600)
    again = server.client.get(f"/api/module/{item}/data").json["services"]
    assert again[1]["since"] == first[1]["since"], "still down: the time it went down is kept"
