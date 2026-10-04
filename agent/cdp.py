"""Minimal Chrome DevTools Protocol client over one browser-level websocket."""
import json
import queue
import threading
import urllib.request

import websocket

# Never route localhost CDP traffic through a proxy.
_local = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CDPError(Exception):
    """Chromium answered with an error. The connection is still usable."""


class CDPDead(Exception):
    """Chromium did not answer or the connection is gone."""


def browser_ws_url(port, timeout=2):
    with _local.open(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
        return json.load(r)["webSocketDebuggerUrl"]


class CDP:
    def __init__(self, port, timeout=10):
        self.timeout = timeout
        self._ws = websocket.create_connection(
            browser_ws_url(port), timeout=timeout, suppress_origin=True,
            http_proxy_host=None, http_no_proxy=["127.0.0.1"])
        self._ws.settimeout(None)
        self._send_lock = threading.Lock()
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending = {}
        self._events = queue.Queue()
        self.closed = False
        threading.Thread(target=self._reader, name=f"cdp-{port}", daemon=True).start()

    def _reader(self):
        try:
            while True:
                msg = json.loads(self._ws.recv())
                if "id" in msg:
                    with self._lock:
                        slot = self._pending.pop(msg["id"], None)
                    if slot:
                        slot["msg"] = msg
                        slot["event"].set()
                else:
                    self._events.put((msg.get("method"), msg.get("params", {}), msg.get("sessionId")))
        except Exception:
            pass
        finally:
            self.closed = True
            with self._lock:
                slots = list(self._pending.values())
                self._pending.clear()
            for slot in slots:
                slot["event"].set()

    def send(self, method, params=None, session=None, timeout=None):
        if self.closed:
            raise CDPDead("connection closed")
        slot = {"event": threading.Event(), "msg": None}
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            self._pending[msg_id] = slot
        payload = {"id": msg_id, "method": method, "params": params or {}}
        if session:
            payload["sessionId"] = session
        try:
            with self._send_lock:
                self._ws.send(json.dumps(payload))
        except Exception as e:
            raise CDPDead(f"send failed: {e}") from e
        if not slot["event"].wait(timeout or self.timeout):
            with self._lock:
                self._pending.pop(msg_id, None)
            raise CDPDead(f"{method} timed out")
        msg = slot["msg"]
        if msg is None:
            raise CDPDead("connection closed")
        if "error" in msg:
            raise CDPError(f"{method}: {msg['error'].get('message')}")
        return msg.get("result", {})

    def events(self):
        """Drain queued events as (method, params, session_id) tuples."""
        while True:
            try:
                yield self._events.get_nowait()
            except queue.Empty:
                return

    def close(self):
        self.closed = True
        try:
            self._ws.close()
        except Exception:
            pass
