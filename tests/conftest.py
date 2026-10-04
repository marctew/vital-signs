"""Shared fixtures. Run from the repo root: python -m pytest"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import config, create_app  # noqa: E402

AGENT = {"Authorization": "Bearer agent-token"}


class Server:
    """A test server with its own empty data folder, and shortcuts for the calls tests repeat."""

    def __init__(self, data_dir, **overrides):
        self.cfg = config.load("no-such-file")
        self.cfg.update(data_dir=data_dir, agent_token="agent-token", **overrides)
        self.data_dir = data_dir
        self.app = create_app(self.cfg)
        self.client = self.app.test_client()

    def poll(self, hostname="pi", displays=None, **extra):
        """Poll as an agent. `displays` is a list of connector names or full status dicts."""
        if displays is None:
            displays = ["HDMI-A-1", "HDMI-A-2"]
        status = [d if isinstance(d, dict) else {"connector": d, "name": {"HDMI-A-1": "Left", "HDMI-A-2": "Right"}.get(d, d)}
                  for d in displays]
        r = self.client.post("/api/agent/poll", headers=AGENT, json={"hostname": hostname, "displays": status, **extra})
        assert r.status_code == 200, r.get_data(as_text=True)
        return r.json

    def post(self, path, **form):
        return self.client.post(path, data=form, follow_redirects=True)

    def db(self):
        return sqlite3.connect(str(self.data_dir / "vitalsigns.db"))

    def ids(self, table="content_items"):
        with self.db() as db:
            return dict(db.execute(f"SELECT name, id FROM {table}").fetchall())

    def add_content(self, name, url=None, **fields):
        form = {"zoom": "1", "refresh_interval": "0", "css": "", **fields}
        self.post("/content", name=name, url=url or f"https://{name.lower()}.example", **form)
        return self.ids()[name]


@pytest.fixture
def server(tmp_path):
    return Server(tmp_path)


@pytest.fixture
def make_server(tmp_path):
    """For tests that need non-default config, or a second app on the same data."""
    return lambda **overrides: Server(tmp_path, **overrides)
