"""Protocol version and helpers shared by the server and the agent.

Bump PROTOCOL_VERSION whenever the agent API in shared/API.md changes in a
way that an older agent or server would not understand.
"""
import os
import subprocess
from pathlib import Path

PROTOCOL_VERSION = 1

REPO_ROOT = Path(__file__).resolve().parent.parent

# How a display moves from one item to the next: name -> milliseconds for each half of the fade.
TRANSITIONS = {"none": 0, "fade": 400, "slow": 1000}

COMMAND_TYPES = ("identify", "reload", "screenshot", "vnc", "next", "previous", "update", "restart_browser", "reboot")


def git_sha():
    """Short SHA of the checked-out commit, or "unknown" outside a git clone."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def start_update(log_path):
    """Run deploy/update.sh in the background, logging to `log_path`.

    The script pulls, installs dependencies and, only if something changed,
    restarts the service that called this. It must never wait for input.
    """
    log = open(log_path, "ab")
    return subprocess.Popen(
        ["bash", str(REPO_ROOT / "deploy" / "update.sh")],
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
