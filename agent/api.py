"""REST client for the server's agent API (see shared/API.md)."""
import json
import logging
import urllib.error
import urllib.request
from urllib.parse import urlencode

log = logging.getLogger("agent.api")


class Api:
    def __init__(self, cfg):
        self.cfg = cfg

    def _post(self, path, body, content_type, timeout):
        req = urllib.request.Request(
            self.cfg.server_url + path, data=body, method="POST",
            headers={"Authorization": f"Bearer {self.cfg.token}", "Content-Type": content_type})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def poll(self, payload):
        """Raises OSError (including URLError/HTTPError) or ValueError when the server is unusable."""
        return self._post("/api/agent/poll", json.dumps(payload).encode(), "application/json", 5)

    def upload_screenshot(self, connector, jpeg, content_id=None):
        """`content_id` says which content item the picture shows, for its preview in the admin UI."""
        params = {"hostname": self.cfg.hostname, "connector": connector}
        if content_id:
            params["content"] = content_id
        query = urlencode(params)
        try:
            self._post(f"/api/agent/screenshot?{query}", jpeg, "image/jpeg", 4)
        except (OSError, ValueError) as e:
            log.debug("screenshot upload failed: %s", e)
