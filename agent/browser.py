"""Launch and supervise one kiosk Chromium for one output."""
import json
import logging
import os
import shutil
import subprocess
import time

from .cdp import CDP, CDPDead, CDPError, browser_ws_url

log = logging.getLogger("agent.browser")

CANDIDATES = ("chromium", "chromium-browser", "google-chrome", "chrome")
LAUNCH_ATTEMPTS = 4
CDP_WAIT = 40
PLACEMENT_WAIT = 10
TOLERANCE = 2


def find_chromium(cfg):
    if cfg.chromium:
        return cfg.chromium
    for name in CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("Chromium not found: set `chromium` in agent.toml")


class Browser:
    def __init__(self, cfg, out, geom):
        self.cfg = cfg
        self.out = out
        self.geom = geom
        self.profile = cfg.state_dir / "profiles" / out.name
        self.proc = None
        self.placement_ok = True

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def memory_mb(self):
        """Resident memory of this Chromium and all its helper processes, in MB. None if unknown."""
        if not self.alive():
            return None
        try:
            page = os.sysconf("SC_PAGE_SIZE")
            children, resident = {}, {}
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    with open(f"/proc/{entry}/stat", encoding="utf-8") as f:
                        fields = f.read().rsplit(")", 1)[1].split()
                    with open(f"/proc/{entry}/statm", encoding="utf-8") as f:
                        resident[int(entry)] = int(f.read().split()[1]) * page
                except (OSError, ValueError, IndexError):
                    continue
                children.setdefault(int(fields[1]), []).append(int(entry))
        except (AttributeError, OSError, ValueError):
            return None         # not Linux
        total, queue = 0, [self.proc.pid]
        while queue:
            pid = queue.pop()
            total += resident.get(pid, 0)
            queue.extend(children.get(pid, []))
        return total // (1024 * 1024)

    def _mark_clean_exit(self):
        """Stop Chromium offering to restore pages after an unclean shutdown."""
        prefs = self.profile / "Default" / "Preferences"
        try:
            data = json.loads(prefs.read_text(encoding="utf-8"))
            data.setdefault("profile", {}).update(exit_type="Normal", exited_cleanly=True)
            prefs.write_text(json.dumps(data), encoding="utf-8")
        except (OSError, ValueError):
            pass

    def _close_stale(self):
        """Close a Chromium left over from an earlier agent run on our CDP port."""
        try:
            browser_ws_url(self.out.cdp_port, timeout=1)
        except Exception:
            return
        log.warning("[%s] closing a stale Chromium on port %s", self.out.name, self.out.cdp_port)
        try:
            cdp = CDP(self.out.cdp_port, timeout=3)
            cdp.send("Browser.close")
            cdp.close()
        except Exception:
            pass
        time.sleep(3)

    def _args(self):
        g, o = self.geom, self.out
        args = [
            find_chromium(self.cfg),
            f"--user-data-dir={self.profile}",
            f"--remote-debugging-port={o.cdp_port}",
            f"--class={o.window_class}",
            "--kiosk",
            f"--window-position={g.x},{g.y}",
            f"--window-size={g.width},{g.height}",
            "--no-first-run",
            "--no-default-browser-check",
            "--noerrdialogs",
            "--disable-infobars",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--disable-features=Translate,InfiniteSessionRestore",
            "--disable-pinch",
            "--overscroll-history-navigation=0",
            "--autoplay-policy=no-user-gesture-required",
            "--password-store=basic",
            "--check-for-update-interval=31536000",
        ]
        if self.cfg.placement == "xwayland":
            args.append("--ozone-platform=x11")
        elif self.cfg.placement == "labwc-rules":
            args.append("--ozone-platform=wayland")
        return args + list(self.cfg.extra_chromium_args) + ["about:blank"]

    def _launch(self):
        self.profile.mkdir(parents=True, exist_ok=True)
        self._mark_clean_exit()
        self.proc = subprocess.Popen(self._args(), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _connect(self):
        deadline = time.monotonic() + CDP_WAIT
        while time.monotonic() < deadline:
            if not self.alive():
                raise CDPDead("Chromium exited during startup")
            try:
                return CDP(self.out.cdp_port)
            except Exception:
                time.sleep(0.5)
        raise CDPDead("Chromium did not open its CDP port")

    def _placed(self, cdp):
        """Read the window bounds back over CDP and compare them with the output."""
        if self.cfg.placement == "none":
            return True
        g = self.geom
        bounds = {}
        deadline = time.monotonic() + PLACEMENT_WAIT
        while time.monotonic() < deadline:
            page = next(t for t in cdp.send("Target.getTargets")["targetInfos"] if t["type"] == "page")
            bounds = cdp.send("Browser.getWindowForTarget", {"targetId": page["targetId"]})["bounds"]
            size_ok = (abs(bounds.get("width", 0) - g.width) <= TOLERANCE
                       and abs(bounds.get("height", 0) - g.height) <= TOLERANCE)
            # A native Wayland client cannot see its own position, so only X11 checks it.
            pos_ok = self.cfg.placement != "xwayland" or (
                abs(bounds.get("left", 0) - g.x) <= TOLERANCE
                and abs(bounds.get("top", 0) - g.y) <= TOLERANCE)
            if size_ok and pos_ok:
                log.info("[%s] window placed: %s", self.out.name, bounds)
                return True
            time.sleep(0.5)
        log.warning("[%s] window is at %s, expected %sx%s at %s,%s",
                    self.out.name, bounds, g.width, g.height, g.x, g.y)
        return False

    def start(self):
        """Launch Chromium and return a connected CDP client. Relaunches if misplaced."""
        cdp = None
        for attempt in range(1, LAUNCH_ATTEMPTS + 1):
            self.stop()
            self._close_stale()
            self._launch()
            cdp = self._connect()
            try:
                self.placement_ok = self._placed(cdp)
            except (CDPError, StopIteration) as e:
                log.warning("[%s] could not read window bounds: %s", self.out.name, e)
                self.placement_ok = False
            if self.placement_ok:
                return cdp
            if attempt < LAUNCH_ATTEMPTS:
                cdp.close()
        log.error("[%s] window still misplaced after %s launches; carrying on",
                  self.out.name, LAUNCH_ATTEMPTS)
        return cdp

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self.proc = None
