"""One Player per display: owns the Chromium window and makes it show the desired state.

Everything that talks to Chromium happens on the player's own thread. Other
threads only hand it state and commands, and read its status snapshot.
"""
import base64
import hashlib
import json
import logging
import threading
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from websocket import WebSocketException

from .browser import Browser
from .cdp import CDPDead, CDPError

log = logging.getLogger("agent.player")

TICK = 0.25
RETRY_SECONDS = 60          # how long before a failed page is tried again
LOADING_GRACE = 30
HEALTH_SECONDS = 10
SHOT_WIDTH = 480
EMPTY_STATE = {"display_zoom": 1.0, "items": [], "override": None}

CSS_INJECTOR = """(function () {
  var css = %s;
  function add() {
    var s = document.createElement('style');
    s.setAttribute('data-vitalsigns', '');
    s.textContent = css;
    (document.head || document.documentElement).appendChild(s);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add);
  else add();
})();"""

IDENTIFY_OVERLAY = """(function (name, ms) {
  var d = document.createElement('div');
  d.textContent = name;
  var s = d.style;
  s.position = 'fixed'; s.left = '0'; s.top = '0'; s.width = '100vw'; s.height = '100vh';
  s.zIndex = '2147483647'; s.background = 'rgba(0,0,0,0.88)'; s.color = '#fff';
  s.display = 'flex'; s.alignItems = 'center'; s.justifyContent = 'center';
  s.fontFamily = 'sans-serif'; s.fontWeight = '700'; s.fontSize = '20vmin';
  (document.body || document.documentElement).appendChild(d);
  setTimeout(function () { d.remove(); }, ms);
})(%s, %d);"""


def idle_url(name):
    html = ("<html><body style='margin:0;height:100vh;background:#000;color:#555;display:flex;"
            "align-items:center;justify-content:center;font:6vmin sans-serif'>"
            f"{name}</body></html>")
    return "data:text/html," + quote(html)


class Tab:
    def __init__(self, key, url, zoom, css, refresh):
        self.key = key
        self.url = url
        self.zoom = zoom
        self.css = css
        self.refresh = refresh
        self.target_id = None
        self.session = None
        self.frame_id = None
        self.doc_request = None
        self.status = "loading"     # loading | ok | error
        self.error = ""
        self.loading_since = 0.0
        self.next_refresh = None
        self.retry_at = None


class Player(threading.Thread):
    def __init__(self, cfg, out, geom, api):
        super().__init__(name=f"player-{out.name}", daemon=True)
        self.cfg = cfg
        self.out = out
        self.geom = geom
        self.api = api
        self.browser = Browser(cfg, out, geom)
        self.cdp = None
        self.screenshot_interval = 30

        self._lock = threading.Lock()
        self._pending_state = None
        self._commands = []
        self._halt = threading.Event()
        self._status = self._build_status()

        self.state = dict(EMPTY_STATE)
        self.finished_override = None   # id of the last override that ended here
        self._reset()

    def _reset(self):
        """Forget everything tied to a Chromium instance."""
        self.tabs = {}              # key -> Tab, one per distinct playlist page
        self.by_session = {}        # CDP session id -> Tab
        self.sessions = {}          # target id -> CDP session id
        self.sequence = []          # [{"key", "duration", "item"}] in playlist order
        self.index = -1
        self.active = None          # target id of the visible tab
        self.idle_target = None
        self.override_tab = None
        self.override_id = None
        self.override_deadline = 0.0
        self.dirty = True
        self.dirty_after = 0.0
        self.next_switch = 0.0
        self.next_shot = 0.0
        self.next_health = 0.0

    # --- called from other threads ---------------------------------------

    def set_state(self, state):
        with self._lock:
            self._pending_state = state

    def enqueue(self, command):
        with self._lock:
            self._commands.append(command)

    def status(self):
        return self._status

    def stop(self):
        self._halt.set()

    # --- main loop ---------------------------------------------------------

    def run(self):
        while not self._halt.is_set():
            try:
                self._ensure_browser()
                now = time.monotonic()
                self._drain_events(now)
                self._apply_state(now)
                self._tick_override(now)
                self._tick_rotation(now)
                self._tick_refresh(now)
                self._run_commands(now)
                self._tick_screenshot(now)
                self._tick_health(now)
            except CDPError as e:
                log.warning("[%s] %s", self.out.name, e)
                self.dirty = True
                self.dirty_after = time.monotonic() + 2
            except (CDPDead, WebSocketException, OSError, RuntimeError) as e:
                log.error("[%s] browser lost (%s); relaunching", self.out.name, e)
                self._teardown()
                self._halt.wait(2)
            self._status = self._build_status()
            self._halt.wait(TICK)
        self._teardown()

    def _teardown(self):
        if self.cdp:
            self.cdp.close()
            self.cdp = None
        self.browser.stop()
        self._reset()

    def _ensure_browser(self):
        if self.cdp and not self.cdp.closed and self.browser.alive():
            return
        self._teardown()
        self._status = self._build_status()
        log.info("[%s] launching Chromium on %s", self.out.name, self.out.connector)
        self.cdp = self.browser.start()
        page = next(t for t in self.cdp.send("Target.getTargets")["targetInfos"] if t["type"] == "page")
        self.idle_target = page["targetId"]
        self._attach(self.idle_target)
        self.cdp.send("Page.navigate", {"url": idle_url(self.out.name)}, self.sessions[self.idle_target])
        self.active = self.idle_target
        self.dirty = True

    def _attach(self, target_id):
        session = self.cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
        self.sessions[target_id] = session
        self.cdp.send("Page.enable", session=session)
        return session

    # --- tabs --------------------------------------------------------------

    def _resolve(self, entry):
        """Absolute URL for an item or override. Local pages get the display's details."""
        url = entry["url"]
        if not entry.get("local"):
            return url
        parts = urlsplit(self.cfg.server_url + url)
        query = parse_qsl(parts.query, keep_blank_values=True) + [
            ("display", self.out.name), ("width", self.geom.width),
            ("height", self.geom.height), ("orientation", self.geom.orientation)]
        return urlunsplit(parts._replace(query=urlencode(query)))

    def _open_tab(self, key, url, zoom, css, refresh, now):
        tab = Tab(key, url, zoom, css, refresh)
        tab.target_id = self.cdp.send("Target.createTarget", {"url": "about:blank", "background": True})["targetId"]
        try:
            tab.session = self._attach(tab.target_id)
            self.by_session[tab.session] = tab
            self.cdp.send("Network.enable", session=tab.session)
            if abs(zoom - 1.0) > 0.001:
                if self.cfg.zoom_method == "emulation":
                    self.cdp.send("Emulation.setDeviceMetricsOverride", {
                        "width": round(self.geom.width / zoom), "height": round(self.geom.height / zoom),
                        "deviceScaleFactor": 0, "mobile": False, "scale": zoom}, tab.session)
                else:
                    css = f"html {{ zoom: {zoom}; }}\n{css}"
            if css:
                self.cdp.send("Page.setBypassCSP", {"enabled": True}, tab.session)
                self.cdp.send("Page.addScriptToEvaluateOnNewDocument",
                              {"source": CSS_INJECTOR % json.dumps(css)}, tab.session)
            self._navigate(tab, now)
        except CDPError:
            self._close_tab(tab)
            raise
        return tab

    def _close_tab(self, tab):
        self.by_session.pop(tab.session, None)
        self.sessions.pop(tab.target_id, None)
        try:
            self.cdp.send("Target.closeTarget", {"targetId": tab.target_id})
        except CDPError:
            pass

    def _navigate(self, tab, now):
        tab.status = "loading"
        tab.error = ""
        tab.loading_since = now
        tab.retry_at = None
        tab.next_refresh = now + tab.refresh if tab.refresh else None
        result = self.cdp.send("Page.navigate", {"url": tab.url}, tab.session, timeout=30)
        tab.frame_id = result.get("frameId")
        if result.get("errorText"):
            self._fail(tab, result["errorText"], now)

    def _fail(self, tab, error, now):
        if tab.status != "error":
            log.warning("[%s] page failed: %s (%s)", self.out.name, tab.url, error)
        tab.status = "error"
        tab.error = error
        tab.retry_at = now + RETRY_SECONDS

    def _activate(self, target_id, now):
        if target_id == self.active:
            return
        self.cdp.send("Target.activateTarget", {"targetId": target_id})
        self.active = target_id
        self.next_shot = min(self.next_shot, now + 2)

    def _drain_events(self, now):
        for method, params, session in self.cdp.events():
            tab = self.by_session.get(session)
            if tab is None:
                continue
            if method == "Network.requestWillBeSent":
                if params.get("type") == "Document" and params.get("frameId") == tab.frame_id:
                    tab.doc_request = params.get("requestId")
            elif method == "Network.responseReceived":
                if params.get("type") == "Document" and params.get("frameId") == tab.frame_id:
                    status = params.get("response", {}).get("status", 200)
                    if status >= 400:
                        self._fail(tab, f"HTTP {status}", now)
                    elif tab.status == "error":
                        tab.status = "loading"
                        tab.loading_since = now
            elif method == "Network.loadingFailed":
                if params.get("requestId") == tab.doc_request and not params.get("canceled"):
                    self._fail(tab, params.get("errorText") or "load failed", now)
            elif method == "Page.loadEventFired":
                if tab.status == "loading":
                    tab.status = "ok"
                    if tab.target_id == self.active:
                        self.next_shot = min(self.next_shot, now + 1)
            elif method == "Inspector.targetCrashed":
                self._fail(tab, "page crashed", now)
                tab.retry_at = now + 5

    # --- desired state -----------------------------------------------------

    def _apply_state(self, now):
        with self._lock:
            pending, self._pending_state = self._pending_state, None
        if pending is not None:
            self.state = pending
            self.dirty = True
        if not self.dirty or now < self.dirty_after:
            return
        self.dirty = False
        self._reconcile(now)

    def _reconcile(self, now):
        display_zoom = float(self.state.get("display_zoom") or 1.0)
        wanted = {}
        sequence = []
        for item in self.state.get("items") or []:
            spec = (self._resolve(item), round(float(item.get("zoom") or 1.0) * display_zoom, 3),
                    item.get("css") or "", int(item.get("refresh") or 0))
            key = hashlib.sha1(json.dumps(spec).encode()).hexdigest()
            wanted[key] = spec
            sequence.append({"key": key, "duration": max(1.0, float(item.get("duration") or 30)), "item": item})

        # Open new tabs before closing old ones so the window never shows a stray tab.
        for key, spec in wanted.items():
            if key not in self.tabs:
                self.tabs[key] = self._open_tab(key, *spec, now)

        current = self.sequence[self.index]["key"] if 0 <= self.index < len(self.sequence) else None
        self.sequence = sequence
        self.index = next((i for i, e in enumerate(sequence) if e["key"] == current), -1)

        self._reconcile_override(now, display_zoom)
        if not self.override_tab:
            if not sequence:
                self._activate(self.idle_target, now)
            elif self.index < 0:
                self._advance(now)

        for key in [k for k in self.tabs if k not in wanted]:
            self._close_tab(self.tabs.pop(key))

    def _reconcile_override(self, now, display_zoom):
        ov = self.state.get("override")
        if ov and ov["id"] == self.finished_override:
            ov = None
        if ov is None:
            if self.override_tab:
                self._end_override(now)
            return
        if ov["id"] == self.override_id:
            return
        old = self.override_tab
        self.override_tab = self._open_tab("override", self._resolve(ov), display_zoom, "", 0, now)
        self.override_id = ov["id"]
        self.override_deadline = now + float(ov.get("remaining_s") or 0)
        self._activate(self.override_tab.target_id, now)
        if old:
            self._close_tab(old)
        log.info("[%s] override: %s", self.out.name, ov["url"])

    def _end_override(self, now):
        """Return to the playlist."""
        tab, self.override_tab = self.override_tab, None
        self.finished_override, self.override_id = self.override_id, None
        if self.sequence and self.index >= 0 and self.tabs[self.sequence[self.index]["key"]].status != "error":
            self._activate(self.tabs[self.sequence[self.index]["key"]].target_id, now)
            self.next_switch = now + self.sequence[self.index]["duration"]
        elif self.sequence:
            self._advance(now)
        else:
            self._activate(self.idle_target, now)
        self._close_tab(tab)
        log.info("[%s] override ended", self.out.name)

    def _tick_override(self, now):
        if self.override_tab and now >= self.override_deadline:
            self._end_override(now)

    # --- rotation ----------------------------------------------------------

    def _advance(self, now):
        """Show the next playlist item whose page loaded, skipping failed ones."""
        count = len(self.sequence)
        for step in range(1, count + 1):
            i = (self.index + step) % count
            tab = self.tabs[self.sequence[i]["key"]]
            if tab.status != "error":
                self.index = i
                self._activate(tab.target_id, now)
                self.next_switch = now + self.sequence[i]["duration"]
                return
        self.index = -1
        self._activate(self.idle_target, now)
        self.next_switch = now + 5

    def _tick_rotation(self, now):
        if self.override_tab or not self.sequence:
            return
        failed = self.index >= 0 and self.tabs[self.sequence[self.index]["key"]].status == "error"
        if failed or now >= self.next_switch:
            self._advance(now)

    def _tick_refresh(self, now):
        usable = sum(1 for t in self.tabs.values() if t.status != "error")
        for tab in list(self.tabs.values()):
            if tab.status == "error":
                if tab.retry_at is not None and now >= tab.retry_at:
                    self._navigate(tab, now)
            elif tab.status == "loading" and now - tab.loading_since > LOADING_GRACE:
                tab.status = "ok"
            elif tab.next_refresh is not None and now >= tab.next_refresh:
                # Reload off screen when the playlist has somewhere else to be.
                if tab.target_id == self.active and usable > 1 and not self.override_tab:
                    continue
                tab.next_refresh = now + tab.refresh
                self.cdp.send("Page.reload", {}, tab.session)

    # --- commands, screenshots, health ---------------------------------------

    def _run_commands(self, now):
        with self._lock:
            commands, self._commands = self._commands, []
        for cmd in commands:
            kind = cmd.get("type")
            if kind == "identify":
                seconds = float((cmd.get("args") or {}).get("seconds") or 5)
                self.cdp.send("Runtime.evaluate", {
                    "expression": IDENTIFY_OVERLAY % (json.dumps(self.out.name), int(seconds * 1000))},
                    self.sessions[self.active])
            elif kind == "reload":
                for tab in list(self.tabs.values()) + ([self.override_tab] if self.override_tab else []):
                    self._navigate(tab, now)
                self.next_shot = now + 3
            elif kind == "screenshot":
                self.next_shot = now

    def _tick_screenshot(self, now):
        if now < self.next_shot:
            return
        self.next_shot = now + self.screenshot_interval
        session = self.sessions.get(self.active)
        if not session:
            return
        try:
            viewport = self.cdp.send("Page.getLayoutMetrics", session=session)["cssLayoutViewport"]
            width, height = viewport["clientWidth"], viewport["clientHeight"]
            shot = self.cdp.send("Page.captureScreenshot", {
                "format": "jpeg", "quality": 60,
                "clip": {"x": 0, "y": 0, "width": width, "height": height,
                         "scale": min(1.0, SHOT_WIDTH / max(1, width))}}, session, timeout=15)
        except CDPError as e:
            log.debug("[%s] screenshot failed: %s", self.out.name, e)
            return
        self.api.upload_screenshot(self.out.connector, base64.b64decode(shot["data"]))

    def _tick_health(self, now):
        if now < self.next_health:
            return
        self.next_health = now + HEALTH_SECONDS
        if not self.browser.alive():
            raise CDPDead("Chromium exited")
        self.cdp.send("Browser.getVersion", timeout=10)

    # --- status --------------------------------------------------------------

    def _build_status(self):
        status = {
            "connector": self.out.connector,
            "name": self.out.name,
            "width": self.geom.width,
            "height": self.geom.height,
            "orientation": self.geom.orientation,
            "browser": "ok" if self.cdp else "down",
            "placement_ok": self.browser.placement_ok,
            "override_active": False,
            "current": None,
            "errors": [],
        }
        if not self.cdp:
            return status
        seen = set()
        for entry in self.sequence:
            tab = self.tabs.get(entry["key"])
            if tab and tab.status == "error" and tab.key not in seen:
                seen.add(tab.key)
                status["errors"].append({"item_id": entry["item"].get("id"), "name": entry["item"].get("name"),
                                         "url": entry["item"].get("url"), "error": tab.error})
        if self.override_tab:
            status["override_active"] = True
            status["current"] = {"override": True, "url": self.override_tab.url}
            if self.override_tab.status == "error":
                status["errors"].append({"name": "override", "url": self.override_tab.url,
                                         "error": self.override_tab.error})
        elif 0 <= self.index < len(self.sequence):
            item = self.sequence[self.index]["item"]
            status["current"] = {"item_id": item.get("id"), "name": item.get("name"), "url": item.get("url")}
        return status
