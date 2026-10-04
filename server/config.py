import os
import tomllib
from pathlib import Path

from shared.protocol import REPO_ROOT

DEFAULT_CONFIG_PATH = "/etc/vitalsigns/server.toml"


def path():
    """The config file the server uses: VITALSIGNS_CONFIG, or the default location."""
    return os.environ.get("VITALSIGNS_CONFIG") or DEFAULT_CONFIG_PATH


def load(path=None):
    cfg = {
        "host": "0.0.0.0",
        "port": 8080,
        "data_dir": str(REPO_ROOT / "data"),
        "agent_token": "",
        "control_token": "",
        "screenshot_interval": 30,
        "max_upload_mb": 50,
        "data_sources": {},
    }
    path = path or globals()["path"]()
    if Path(path).is_file():
        with open(path, "rb") as f:
            cfg.update(tomllib.load(f))
    cfg["data_dir"] = Path(cfg["data_dir"])
    return cfg
