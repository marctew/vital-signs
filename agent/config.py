import os
import re
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PLACEMENTS = ("xwayland", "labwc-rules", "none")
ZOOM_METHODS = ("emulation", "css")


def default_config_path():
    return Path(os.environ.get("VITALSIGNS_AGENT_CONFIG")
                or Path.home() / ".config" / "vitalsigns" / "agent.toml")


@dataclass
class Output:
    connector: str
    name: str
    cdp_port: int
    rotation: int = 0
    x: int = 0
    y: int = 0
    mode: str = ""          # optional, e.g. "2560x1080@60Hz"; pinned in kanshi when set
    width: int = 0          # only used when detect_outputs is false
    height: int = 0

    @property
    def window_class(self):
        return "vitalsigns-" + re.sub(r"[^A-Za-z0-9_-]", "-", self.name)


@dataclass
class Config:
    server_url: str
    token: str
    hostname: str
    outputs: list
    poll_interval: float = 2.0
    chromium: str = ""
    placement: str = "xwayland"
    zoom_method: str = "emulation"
    detect_outputs: bool = True
    cursor_hide_seconds: float = 3.0
    browser_memory_limit_mb: int = 3000
    state_dir: Path = field(default_factory=lambda: Path.home() / ".local" / "state" / "vitalsigns")
    extra_chromium_args: list = field(default_factory=list)


def load(path=None):
    path = Path(path) if path else default_config_path()
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    outputs = []
    for i, o in enumerate(raw.get("outputs", [])):
        rotation = int(o.get("rotation", 0))
        if rotation not in (0, 90, 180, 270):
            raise ValueError(f"outputs[{i}].rotation must be 0, 90, 180 or 270")
        outputs.append(Output(
            connector=o["connector"],
            name=o.get("name") or o["connector"],
            cdp_port=int(o.get("cdp_port", 9222 + i)),
            rotation=rotation,
            x=int(o.get("x", 0)),
            y=int(o.get("y", 0)),
            mode=o.get("mode", ""),
            width=int(o.get("width", 0)),
            height=int(o.get("height", 0)),
        ))
    if not outputs:
        raise ValueError(f"{path}: configure at least one [[outputs]] entry")
    for attr in ("connector", "name", "cdp_port"):
        values = [getattr(o, attr) for o in outputs]
        if len(set(values)) != len(values):
            raise ValueError(f"{path}: every output needs a distinct {attr}")

    cfg = Config(
        server_url=raw["server_url"].rstrip("/"),
        token=raw["token"],
        hostname=raw.get("hostname") or socket.gethostname(),
        outputs=outputs,
        poll_interval=float(raw.get("poll_interval", 2.0)),
        chromium=raw.get("chromium", ""),
        placement=raw.get("placement", "xwayland"),
        zoom_method=raw.get("zoom_method", "emulation"),
        detect_outputs=bool(raw.get("detect_outputs", True)),
        cursor_hide_seconds=float(raw.get("cursor_hide_seconds", 3.0)),
        browser_memory_limit_mb=int(raw.get("browser_memory_limit_mb", 3000)),
        extra_chromium_args=list(raw.get("extra_chromium_args", [])),
    )
    if raw.get("state_dir"):
        cfg.state_dir = Path(raw["state_dir"]).expanduser()
    if cfg.placement not in PLACEMENTS:
        raise ValueError(f"placement must be one of {PLACEMENTS}")
    if cfg.zoom_method not in ZOOM_METHODS:
        raise ValueError(f"zoom_method must be one of {ZOOM_METHODS}")
    return cfg
