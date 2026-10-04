"""Vital Signs agent. Run from the repo root:

    python -m agent                     run the agent
    python -m agent probe               print what the agent can see (for docs/ verification)
    python -m agent render-kanshi       print the kanshi profile for the configured outputs
    python -m agent render-labwc-rules  print labwc window rules for the "labwc-rules" placement
"""
import argparse
import collections
import json
import logging
import os
import signal
import subprocess
import sys
import threading

from shared.protocol import PROTOCOL_VERSION, git_sha, start_update

from . import config, health, outputs
from .api import Api
from .browser import find_chromium
from .player import EMPTY_STATE, Player

log = logging.getLogger("agent")


def load_cache(path):
    """Last known desired state. Overrides are dropped: how long they have left is unknown."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for display in state.values():
        display["override"] = None
        display["power_override"] = None
    return state


def save_cache(path, state):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)


def run(cfg):
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cfg.state_dir / "desired-state.json"
    sha = git_sha()
    api = Api(cfg)

    geometry = outputs.wait_for(cfg)
    for out in cfg.outputs:
        g = geometry[out.connector]
        log.info("Output %s (%s): %sx%s at %s,%s %s", out.connector, out.name,
                 g.width, g.height, g.x, g.y, g.orientation)

    cursor_hider = outputs.start_cursor_hider(cfg.cursor_hide_seconds) if cfg.detect_outputs else None

    cached = load_cache(cache_path)
    players = {}
    for out in cfg.outputs:
        player = Player(cfg, out, geometry[out.connector], api)
        player.set_state(cached.get(out.connector, dict(EMPTY_STATE)))
        player.start()
        players[out.connector] = player

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    revision = None     # None on the first poll, so the server always sends fresh state
    acks = []
    seen = collections.deque(maxlen=500)
    online = None
    # An update or a reboot stops the agent. It only starts once the server has the
    # command's ack, or the command would be delivered again after the restart.
    requested, acked = [], []
    while not stop.is_set():
        payload = {
            "hostname": cfg.hostname,
            "git_sha": sha,
            "protocol_version": PROTOCOL_VERSION,
            "revision": revision,
            "acks": acks,
            "displays": [p.status() for p in players.values()],
            "health": health.read(),
        }
        try:
            resp = api.poll(payload)
        except (OSError, ValueError) as e:
            if online is not False:
                log.warning("Server unreachable (%s); continuing with the last known state", e)
            online = False
            stop.wait(cfg.poll_interval)
            continue
        if online is not True:
            log.info("Connected to %s", cfg.server_url)
            if resp.get("protocol_version") != PROTOCOL_VERSION:
                log.warning("Protocol mismatch: agent %s, server %s. Run deploy/update.sh on both.",
                            PROTOCOL_VERSION, resp.get("protocol_version"))
        online = True
        acks = []
        for action in acked:
            if action == "update":
                log.info("Updating from the repo (log: %s)", cfg.state_dir / "update.log")
                start_update(cfg.state_dir / "update.log")
            elif action == "reboot":
                log.info("Rebooting on request")
                if not health.reboot():
                    log.error("Could not reboot: neither systemctl reboot nor sudo is allowed for this user")
        acked, requested = requested, []

        if "displays" in resp:
            for connector, player in players.items():
                player.set_state(resp["displays"].get(connector, dict(EMPTY_STATE)))
            save_cache(cache_path, resp["displays"])
        revision = resp.get("revision")

        for cmd in resp.get("commands", []):
            acks.append(cmd["id"])
            if cmd["id"] in seen:
                continue
            seen.append(cmd["id"])
            if cmd.get("type") in ("update", "reboot"):
                requested.append(cmd["type"])
                continue
            if cmd.get("type") == "vnc":
                outputs.vnc_show(cmd.get("connector") or cfg.outputs[0].connector)
                continue
            for connector, player in players.items():
                if cmd.get("connector") in (None, connector):
                    player.enqueue(cmd)

        for player in players.values():
            player.screenshot_interval = max(5, float(resp.get("screenshot_interval") or 30))
        stop.wait(float(resp.get("poll_interval") or cfg.poll_interval))

    log.info("Stopping")
    if cursor_hider:
        cursor_hider.terminate()
    for player in players.values():
        player.stop()
    for player in players.values():
        player.join(15)


def probe(cfg):
    """Print the facts docs/on-device-verification.md asks for."""
    def show(title, cmd):
        print(f"\n## {title}\n$ {' '.join(cmd)}")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            print((r.stdout or r.stderr).strip())
        except (OSError, subprocess.SubprocessError) as e:
            print(f"(failed: {e})")

    print("## Session")
    for var in ("XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "WAYLAND_DISPLAY", "DISPLAY"):
        print(f"{var}={os.environ.get(var, '')}")
    show("OS", ["sh", "-c", ". /etc/os-release; echo $PRETTY_NAME"])
    show("labwc", ["labwc", "--version"])
    show("wayvnc", ["wayvnc", "--version"])
    try:
        show("Chromium", [find_chromium(cfg), "--version"])
    except RuntimeError as e:
        print(f"\n## Chromium\n{e}")
    show("Outputs (wlr-randr)", ["wlr-randr"])
    print("\n## Outputs as the agent sees them")
    found = outputs.detect()
    for name, g in found.items():
        print(f"{name}: {g.width}x{g.height} at {g.x},{g.y} transform={g.transform} {g.orientation}")
    for out in cfg.outputs:
        if out.connector not in found:
            print(f"MISSING: configured output {out.connector} ({out.name}) was not detected")


def main():
    parser = argparse.ArgumentParser(prog="python -m agent", description="Vital Signs agent")
    parser.add_argument("command", nargs="?", default="run",
                        choices=("run", "probe", "render-kanshi", "render-labwc-rules"))
    parser.add_argument("--config", help="path to agent.toml")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = config.load(args.config)
    except (OSError, KeyError, ValueError) as e:
        sys.exit(f"Config error: {e!r}")

    if args.command == "render-kanshi":
        sys.stdout.write(outputs.render_kanshi(cfg))
    elif args.command == "render-labwc-rules":
        sys.stdout.write(outputs.render_labwc_rules(cfg))
    elif args.command == "probe":
        probe(cfg)
    else:
        run(cfg)


if __name__ == "__main__":
    main()
