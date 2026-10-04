"""The agent's side of the server: registration, desired state, commands, screenshots."""
from conftest import AGENT

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


def test_agent_needs_the_token(server):
    assert server.client.post("/api/agent/poll", json={"hostname": "pi"}).status_code == 401
    assert server.client.post("/api/agent/poll", json={"hostname": "pi"},
                              headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_first_poll_registers_agent_and_displays(server):
    reply = server.poll()
    assert set(reply["displays"]) == {"HDMI-A-1", "HDMI-A-2"}
    assert reply["displays"]["HDMI-A-1"]["items"] == []
    listed = server.client.get("/api/v1/displays").json["displays"]
    assert [(d["ref"], d["online"]) for d in listed] == [("pi:Left", True), ("pi:Right", True)]


def test_unchanged_state_is_not_sent_again(server):
    first = server.poll()
    again = server.poll(revision=first["revision"])
    assert "displays" not in again and again["revision"] == first["revision"]


def test_assignment_changes_the_desired_state(server):
    revision = server.poll()["revision"]
    content_id = server.add_content("News", zoom="1.25", css="body{}")
    server.post("/playlists", name="Main")
    server.post("/playlists/1/items", content_id=str(content_id), duration="45")
    assert server.client.put("/api/v1/displays/pi:Left/assignment", json={"playlist": "Main"}).status_code == 200
    reply = server.poll(revision=revision)
    item = reply["displays"]["HDMI-A-1"]["items"][0]
    assert (item["name"], item["url"], item["duration"], item["zoom"], item["local"]) == \
        ("News", "https://news.example", 45, 1.25, False)
    assert reply["displays"]["HDMI-A-2"]["items"] == []


def test_override_is_delivered_with_time_left_and_can_be_cleared(server):
    server.poll()
    r = server.client.post("/api/v1/displays/1/override", json={"url": "/pages/clock/?title=Hi", "minutes": 2})
    assert r.status_code == 200
    override = server.poll()["displays"]["HDMI-A-1"]["override"]
    assert override["local"] is True and 100 < override["remaining_s"] <= 120
    assert server.client.delete("/api/v1/displays/1/override").status_code == 200
    assert server.poll()["displays"]["HDMI-A-1"]["override"] is None
    assert server.client.post("/api/v1/displays/1/override", json={"url": "file:///etc/passwd"}).status_code == 400


def test_commands_are_redelivered_until_acknowledged(server):
    server.poll()
    for action in ("identify", "reload", "next", "previous", "vnc"):
        assert server.client.post(f"/api/v1/displays/pi:Left/{action}").status_code == 200
    commands = server.poll()["commands"]
    assert [c["type"] for c in commands] == ["identify", "reload", "next", "previous", "vnc"]
    assert all(c["connector"] == "HDMI-A-1" for c in commands)
    assert len(server.poll()["commands"]) == 5, "still pending without an ack"
    assert server.poll(acks=[c["id"] for c in commands])["commands"] == []


def test_screenshot_upload_and_content_preview(server):
    server.poll()
    content_id = server.add_content("News")
    url = "/api/agent/screenshot?hostname=pi&connector=HDMI-A-1"
    assert server.client.post(url, data=JPEG).status_code == 401
    assert server.client.post(url, data=b"not a jpeg", headers=AGENT).status_code == 400
    assert server.client.post("/api/agent/screenshot?hostname=pi&connector=nope", data=JPEG, headers=AGENT).status_code == 404
    assert server.client.post(f"{url}&content={content_id}", data=JPEG, headers=AGENT).status_code == 200
    assert server.client.get("/screenshots/1.jpg").data == JPEG
    assert server.client.get(f"/previews/{content_id}.jpg").data == JPEG
    assert f"/previews/{content_id}.jpg" in server.client.get("/content").text
    # an unknown content id is ignored rather than stored
    assert server.client.post(f"{url}&content=999", data=JPEG, headers=AGENT).status_code == 200
    assert server.client.get("/previews/999.jpg").status_code == 404


def test_update_all_queues_an_update_for_every_agent(server, monkeypatch):
    import server.ui as ui
    started = []
    monkeypatch.setattr(ui, "start_update", lambda log_path: started.append(log_path))
    server.poll("pi-one")
    server.poll("pi-two")
    page = server.post("/update").text
    assert "Update started on the server and 2 agent(s)" in page and len(started) == 1
    assert [(c["type"], c["connector"]) for c in server.poll("pi-one")["commands"]] == [("update", None)]
