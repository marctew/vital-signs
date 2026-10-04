"""Output detection through wlr-randr, and compositor config rendering."""
import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass

log = logging.getLogger("agent.outputs")

SETTLE_SECONDS = 20


@dataclass
class Geometry:
    """Logical geometry of an output, after rotation and scaling."""
    x: int
    y: int
    width: int
    height: int
    transform: str = "normal"

    @property
    def orientation(self):
        return "portrait" if self.height > self.width else "landscape"


def _logical(mode_w, mode_h, transform, scale):
    if "90" in transform or "270" in transform:
        mode_w, mode_h = mode_h, mode_w
    scale = scale or 1.0
    return round(mode_w / scale), round(mode_h / scale)


def _parse_json(text):
    found = {}
    for o in json.loads(text):
        mode = next((m for m in o.get("modes", []) if m.get("current")), None)
        if not o.get("enabled") or mode is None:
            continue
        transform = str(o.get("transform", "normal"))
        w, h = _logical(mode["width"], mode["height"], transform, o.get("scale", 1.0))
        pos = o.get("position", {})
        found[o["name"]] = Geometry(int(pos.get("x", 0)), int(pos.get("y", 0)), w, h, transform)
    return found


def _parse_text(text):
    """Parse plain `wlr-randr` output, for versions without --json."""
    found = {}
    blocks = re.split(r"^(?=\S)", text, flags=re.M)
    for block in blocks:
        if not block.strip():
            continue
        name = block.split()[0]
        if not re.search(r"Enabled:\s*yes", block):
            continue
        mode = re.search(r"(\d+)x(\d+) px[^\n]*current", block)
        if not mode:
            continue
        pos = re.search(r"Position:\s*(-?\d+),(-?\d+)", block)
        transform = re.search(r"Transform:\s*(\S+)", block)
        scale = re.search(r"Scale:\s*([\d.]+)", block)
        transform = transform.group(1) if transform else "normal"
        w, h = _logical(int(mode.group(1)), int(mode.group(2)), transform,
                        float(scale.group(1)) if scale else 1.0)
        found[name] = Geometry(int(pos.group(1)) if pos else 0, int(pos.group(2)) if pos else 0,
                               w, h, transform)
    return found


def detect():
    """Enabled outputs as {connector: Geometry}. Empty if wlr-randr is unavailable."""
    try:
        r = subprocess.run(["wlr-randr", "--json"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return _parse_json(r.stdout)
        r = subprocess.run(["wlr-randr"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return _parse_text(r.stdout)
        log.debug("wlr-randr failed: %s", r.stderr.strip())
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as e:
        log.debug("wlr-randr unavailable: %s", e)
    return {}


# Raspberry Pi OS runs wayvnc as a system service under the "vnc" user, with a
# control socket only that user can write to, so the last attempt goes through sudo.
WAYVNC_SOCKET = "/tmp/wayvnc/wayvncctl.sock"
WAYVNCCTL = (
    ["wayvncctl"],
    ["wayvncctl", f"--socket={WAYVNC_SOCKET}"],
    ["sudo", "-n", "-u", "vnc", "wayvncctl", f"--socket={WAYVNC_SOCKET}"],
)


def vnc_show(connector):
    """Point wayvnc at an output, so a VNC client sees that monitor."""
    error = "wayvncctl not found"
    for prefix in WAYVNCCTL:
        try:
            r = subprocess.run(prefix + ["output-set", connector], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as e:
            error = str(e)
            continue
        if r.returncode == 0:
            log.info("VNC now shows %s", connector)
            return True
        error = (r.stderr or r.stdout).strip()
    log.warning("Could not switch VNC to %s: %s", connector, error)
    return False


def period_on(period, when):
    """Whether one {"on", "off", "days"} period covers local time `when`."""
    on, off, days = period["on"], period["off"], period["days"]
    clock, today = when.strftime("%H:%M"), when.weekday()
    if on < off:
        return today in days and on <= clock < off
    return (today in days and clock >= on) or ((today - 1) % 7 in days and clock < off)


def scheduled_on(periods, when):
    """Whether a screen should be powered at local time `when`.

    `periods` is a list of {"on", "off", "days"} (Monday is 0). The screen is on
    during any of them; when `off` is earlier than `on`, the period runs past
    midnight into the next day. No periods means always on.
    """
    if not periods:
        return True
    if isinstance(periods, dict):   # state cached by a version with one schedule per display
        periods = [periods]
    try:
        return any(period_on(p, when) for p in periods)
    except (KeyError, TypeError):
        return True


def set_power(connector, on):
    """Power an output's monitor on or off (DPMS) without changing the desktop layout."""
    try:
        r = subprocess.run(["wlopm", "--on" if on else "--off", connector],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("Could not switch %s %s: %s (is wlopm installed? re-run deploy/install-agent.sh)",
                    connector, "on" if on else "off", e)
        return False
    if r.returncode != 0:
        log.warning("Could not switch %s %s: %s", connector, "on" if on else "off",
                    (r.stderr or r.stdout).strip())
        return False
    return True


def start_cursor_hider(seconds):
    """Park the pointer off the corner of the desktop whenever it has been idle.

    Chromium cannot hide the pointer under XWayland on labwc, so this is done at
    the compositor: swayidle notices the idle time and wlrctl moves the pointer.
    Returns the swayidle process, or None when hiding is off or unavailable.
    """
    if seconds <= 0:
        return None
    missing = [tool for tool in ("swayidle", "wlrctl") if not shutil.which(tool)]
    if missing:
        log.warning("Cursor hiding needs %s: re-run deploy/install-agent.sh", " and ".join(missing))
        return None
    return subprocess.Popen(
        ["swayidle", "-w", "timeout", str(max(1, round(seconds))), "wlrctl pointer move 10000 10000"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _transform_name(rotation):
    return "normal" if rotation == 0 else str(rotation)


def wait_for(cfg):
    """Block until every configured output is detected. Returns {connector: Geometry}.

    Also waits a little for the compositor to apply the pinned rotation and
    position, so windows are not placed against a layout that is about to change.
    """
    if not cfg.detect_outputs:
        return {o.connector: Geometry(o.x, o.y, o.width or 1920, o.height or 1080,
                                      _transform_name(o.rotation)) for o in cfg.outputs}
    started = time.monotonic()
    last_log = 0.0
    all_seen_at = None
    while True:
        found = detect()
        missing = [o.connector for o in cfg.outputs if o.connector not in found]
        now = time.monotonic()
        if not missing:
            all_seen_at = all_seen_at or now
            unsettled = [o.connector for o in cfg.outputs
                         if found[o.connector].transform != _transform_name(o.rotation)
                         or (found[o.connector].x, found[o.connector].y) != (o.x, o.y)]
            if not unsettled:
                return {o.connector: found[o.connector] for o in cfg.outputs}
            if now - all_seen_at > SETTLE_SECONDS:
                log.warning("Outputs %s do not match the configured rotation/position; "
                            "using the layout the compositor reports", ", ".join(unsettled))
                return {o.connector: found[o.connector] for o in cfg.outputs}
        elif now - last_log > 10:
            last_log = now
            log.info("Waiting for outputs: %s (%.0fs)", ", ".join(missing), now - started)
        time.sleep(1)


def render_kanshi(cfg):
    """kanshi profile pinning each output's position and rotation."""
    lines = ["# Written by Vital Signs (deploy/install-agent.sh). Edit agent.toml, then re-run it.",
             "profile vitalsigns {"]
    for o in cfg.outputs:
        mode = f" mode {o.mode}" if o.mode else ""
        lines.append(f"    output {o.connector} enable{mode} position {o.x},{o.y}"
                     f" transform {_transform_name(o.rotation)}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_labwc_rules(cfg):
    """labwc window rules for the "labwc-rules" placement. Goes inside <windowRules> in rc.xml."""
    lines = ["<windowRules>"]
    for o in cfg.outputs:
        lines += [
            f'  <windowRule identifier="{o.window_class}" serverDecoration="no">',
            f'    <action name="MoveToOutput" output="{o.connector}" />',
            '    <action name="ToggleFullscreen" />',
            "  </windowRule>",
        ]
    lines.append("</windowRules>")
    return "\n".join(lines) + "\n"
