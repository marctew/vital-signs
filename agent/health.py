"""Readings about the Pi itself, sent with each poll: temperature, memory, disk, network, power.

Every reading is optional. Anything that cannot be read on this machine is left
out, so the same code runs on a Pi and on a development PC.
"""
import os
import re
import shutil
import subprocess
import time

REFRESH_SECONDS = 10
_last = (0.0, {})


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _run(command):
    return subprocess.run(command, capture_output=True, text=True, timeout=3).stdout


def _temperature():
    return round(int(_read("/sys/class/thermal/thermal_zone0/temp")) / 1000, 1)


def _memory():
    info = dict(re.findall(r"^(\w+):\s+(\d+) kB", _read("/proc/meminfo"), re.M))
    total, available = int(info["MemTotal"]), int(info["MemAvailable"])
    return {"memory_total_mb": round(total / 1024), "memory_used_percent": round(100 * (total - available) / total)}


def _uptime():
    return int(float(_read("/proc/uptime").split()[0]))


def _disk():
    usage = shutil.disk_usage("/")
    return round(100 * usage.used / usage.total)


def _network():
    """How the Pi reaches the network: {"network": "wifi" | "ethernet", "interface", "wifi_signal_dbm"}."""
    route = _run(["ip", "route", "get", "1.1.1.1"])
    interface = re.search(r"\bdev (\S+)", route).group(1)
    wireless = os.path.isdir(f"/sys/class/net/{interface}/wireless")
    result = {"network": "wifi" if wireless else "ethernet", "interface": interface}
    if wireless:
        for line in _read("/proc/net/wireless").splitlines():
            if line.strip().startswith(interface + ":"):
                result["wifi_signal_dbm"] = round(float(line.split()[3]))
    return result


def _power():
    """Raspberry Pi firmware flags: is the supply sagging or the CPU being slowed right now, or has it been."""
    flags = int(_run(["vcgencmd", "get_throttled"]).strip().split("=")[1], 16)
    return {"under_voltage": bool(flags & 0x1), "throttled": bool(flags & 0x4),
            "under_voltage_seen": bool(flags & 0x10000), "throttled_seen": bool(flags & 0x40000)}


def read():
    """Current readings, refreshed at most every REFRESH_SECONDS."""
    global _last
    if time.monotonic() - _last[0] < REFRESH_SECONDS and _last[1]:
        return _last[1]
    health = {}
    for key, reader in (("temperature_c", _temperature), ("uptime_s", _uptime), ("disk_used_percent", _disk),
                        ("load", lambda: round(os.getloadavg()[0], 2))):
        try:
            health[key] = reader()
        except (OSError, ValueError, AttributeError, IndexError):
            pass
    for reader in (_memory, _network, _power):
        try:
            health.update(reader())
        except (OSError, ValueError, KeyError, AttributeError, IndexError, subprocess.SubprocessError):
            pass
    _last = (time.monotonic(), health)
    return health


def reboot():
    """Restart the Pi. Tries the session's own permission first, then passwordless sudo."""
    for command in (["systemctl", "reboot"], ["sudo", "-n", "systemctl", "reboot"]):
        try:
            if subprocess.run(command, capture_output=True, timeout=10).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False
