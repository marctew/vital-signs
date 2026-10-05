"""Backup and restore of the database, uploads, password and config."""
import gc
import io
import tarfile

import pytest

from server import auth, backup


def test_backup_and_restore_round_trip(server, tmp_path):
    config_path = tmp_path / "server.toml"
    config_path.write_text('agent_token = "original"\n')
    server.add_content("News")
    server.client.post("/local-pages/upload", data={"name": "bulletin", "file": (io.BytesIO(b"<p>hi</p>"), "n.html")},
                       content_type="multipart/form-data")
    auth.set_password(server.data_dir, "correct horse")
    (server.data_dir / "screenshots" / "1.jpg").write_bytes(b"big")
    (server.data_dir / "photos" / "Holidays").mkdir(parents=True)
    (server.data_dir / "photos" / "Holidays" / "beach.jpg").write_bytes(b"jpeg")

    archive = backup.make_backup(server.cfg, config_path)
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert {"data/vitalsigns.db", "data/admin-password.hash", "data/secret-key", "data/pages/bulletin/index.html",
            "data/photos/Holidays/beach.jpg", "config/server.toml"} <= names
    assert not any("screenshots" in n or "backups" in n for n in names), "regenerable files are left out"

    # lose everything, then restore
    db = server.db()
    db.execute("DELETE FROM content_items")
    db.commit()
    db.close()
    gc.collect()    # restore replaces the database file, so nothing may hold it open (as with the server stopped)
    (server.data_dir / "pages" / "bulletin" / "index.html").write_text("changed")
    (server.data_dir / "pages" / "extra").mkdir()
    auth.clear_password(server.data_dir)
    config_path.write_text('agent_token = "changed"\n')

    assert backup.restore_backup(server.cfg, config_path, archive) is True
    assert "News" in server.ids()
    assert (server.data_dir / "pages" / "bulletin" / "index.html").read_text() == "<p>hi</p>"
    assert not (server.data_dir / "pages" / "extra").exists(), "pages added after the backup are gone"
    assert (server.data_dir / "photos" / "Holidays" / "beach.jpg").read_bytes() == b"jpeg"
    assert (server.data_dir / "admin-password.hash").is_file()
    assert "original" in config_path.read_text()


def test_restore_can_leave_the_config_alone(server, tmp_path):
    config_path = tmp_path / "server.toml"
    config_path.write_text("a = 1\n")
    archive = backup.make_backup(server.cfg, config_path)
    config_path.write_text("a = 2\n")
    assert backup.restore_backup(server.cfg, config_path, archive, restore_config=False) is False
    assert config_path.read_text() == "a = 2\n"


def test_old_backups_are_pruned(server, tmp_path, monkeypatch):
    stamps = iter(["vitalsigns-20260101-000000.tar.gz", "vitalsigns-20260102-000000.tar.gz", "vitalsigns-20260103-000000.tar.gz"])
    monkeypatch.setattr(backup.time, "strftime", lambda fmt: next(stamps))
    dest = tmp_path / "out"
    for _ in range(3):
        backup.make_backup(server.cfg, None, dest, keep=2)
    assert sorted(p.name for p in dest.iterdir()) == ["vitalsigns-20260102-000000.tar.gz", "vitalsigns-20260103-000000.tar.gz"]


def test_restore_refuses_archives_that_are_not_ours(server, tmp_path):
    def archive_with(name):
        path = tmp_path / "bad.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        return path

    for name in ("../evil", "data/../../evil", "etc/passwd", "data/other.txt"):
        with pytest.raises(ValueError):
            backup.restore_backup(server.cfg, None, archive_with(name))
    assert not (tmp_path.parent / "evil").exists()
