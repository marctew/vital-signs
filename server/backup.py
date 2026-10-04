"""Back up and restore everything that cannot be recreated from the repo.

A backup is one .tar.gz holding the database, uploaded pages, the admin
password hash, the session key and the server config. Screenshots and content
previews are left out: the displays send new ones. So is the modules' saved data.
"""
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

DATABASE = "vitalsigns.db"
DATA_FILES = ("admin-password.hash", "secret-key")
PREFIX = "vitalsigns-"


def default_dir(cfg):
    return cfg["data_dir"] / "backups"


def make_backup(cfg, config_path, dest_dir=None, keep=0):
    """Write a backup into dest_dir and return its path. keep > 0 deletes all but the newest `keep`."""
    data_dir = cfg["data_dir"]
    dest_dir = Path(dest_dir) if dest_dir else default_dir(cfg)
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive = dest_dir / time.strftime(f"{PREFIX}%Y%m%d-%H%M%S.tar.gz")
    with tempfile.TemporaryDirectory() as tmp:
        # Copy the database through SQLite so a backup taken while the server runs is consistent.
        snapshot = Path(tmp) / DATABASE
        source, target = sqlite3.connect(data_dir / DATABASE), sqlite3.connect(snapshot)
        with target:
            source.backup(target)
        source.close()
        target.close()
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(snapshot, f"data/{DATABASE}")
            for name in DATA_FILES:
                if (data_dir / name).is_file():
                    tar.add(data_dir / name, f"data/{name}")
            if (data_dir / "pages").is_dir():
                tar.add(data_dir / "pages", "data/pages")
            if config_path and Path(config_path).is_file():
                tar.add(config_path, "config/server.toml")
    if os.name == "posix":
        os.chmod(archive, 0o600)        # it holds tokens and the password hash
    if keep > 0:
        for old in sorted(dest_dir.glob(f"{PREFIX}*.tar.gz"))[:-keep]:
            old.unlink()
    return archive


def _safe_members(tar):
    members = []
    for member in tar.getmembers():
        parts = Path(member.name).parts
        inside = member.name == "config/server.toml" or (parts and parts[0] == "data")
        if not inside or ".." in parts or member.name.startswith(("/", "\\")) or not (member.isreg() or member.isdir()):
            raise ValueError(f"Not a Vital Signs backup: unexpected entry {member.name!r}")
        members.append(member)
    if not any(m.name == f"data/{DATABASE}" for m in members):
        raise ValueError("Not a Vital Signs backup: it has no database")
    return members


def restore_backup(cfg, config_path, archive, restore_config=True):
    """Replace the current data with a backup's. The server must not be running."""
    data_dir = cfg["data_dir"]
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(archive) as tar:
        members = _safe_members(tar)
        if hasattr(tarfile, "data_filter"):
            tar.extractall(tmp, members=members, filter="data")
        else:
            tar.extractall(tmp, members=members)
        unpacked = Path(tmp)

        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            for leftover in (DATABASE + "-wal", DATABASE + "-shm"):     # belong to the database being replaced
                (data_dir / leftover).unlink(missing_ok=True)
        except PermissionError:
            raise OSError("the database is in use; stop the server first") from None
        shutil.copy2(unpacked / "data" / DATABASE, data_dir / DATABASE)
        for name in DATA_FILES:
            if (unpacked / "data" / name).is_file():
                shutil.copy2(unpacked / "data" / name, data_dir / name)
            else:
                (data_dir / name).unlink(missing_ok=True)
        shutil.rmtree(data_dir / "pages", ignore_errors=True)
        if (unpacked / "data" / "pages").is_dir():
            shutil.copytree(unpacked / "data" / "pages", data_dir / "pages")
        restored_config = restore_config and config_path and (unpacked / "config" / "server.toml").is_file()
        if restored_config:
            Path(config_path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(unpacked / "config" / "server.toml", config_path)

    if os.name == "posix":      # the service may run as a different user from whoever restores
        owner = data_dir.stat()
        for path in [data_dir / DATABASE, *(data_dir / n for n in DATA_FILES), *(data_dir / "pages").rglob("*"), data_dir / "pages"]:
            if path.exists():
                os.chown(path, owner.st_uid, owner.st_gid)
    return bool(restored_config)
