"""Protocol version and helpers shared by the server and the agent.

Bump PROTOCOL_VERSION whenever the agent API in shared/API.md changes in a
way that an older agent or server would not understand.
"""
import subprocess
from pathlib import Path

PROTOCOL_VERSION = 1

REPO_ROOT = Path(__file__).resolve().parent.parent

COMMAND_TYPES = ("identify", "reload", "screenshot")


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
