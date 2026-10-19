from pathlib import Path

import pytest

from app.local import file_lock
from app.services.maintenance import BackupWorker


class Process:
    def __init__(self, code=0):
        self.returncode = code

    async def communicate(self):
        return b"", b"private diagnostic not forwarded"

    def kill(self):
        self.returncode = -9


async def test_backup_is_atomic_verified_and_password_not_in_argv(settings, monkeypatch):
    calls = []

    async def create(*command, **kwargs):
        calls.append((command, kwargs))
        if "pg_dump" in command[0]:
            path = Path(command[command.index("--file") + 1])
            assert path.suffix == ".partial"
            assert list(settings.backups_dir.glob("*.dump")) == []
            path.write_bytes(b"PGDMP" + b"x" * 200)
        return Process()

    monkeypatch.setattr("app.services.maintenance.shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("app.services.maintenance.asyncio.create_subprocess_exec", create)
    worker = BackupWorker(settings)
    path = await worker.create_backup()
    assert path.exists() and path.suffix == ".dump"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(settings.backups_dir.glob("*.partial"))
    assert worker.state()["catalog_verified"]
    assert calls[0][1]["env"]["PGPASSWORD"] == "unused"
    assert not any("unused:" in str(arg) for command, _ in calls for arg in command)
    assert any("--list" in command for command, _ in calls)


async def test_failed_dump_preserves_previous_backup_and_success_time(settings, monkeypatch):
    old = settings.backups_dir / "tgviewer_previous.dump"
    old.write_bytes(b"old data")
    worker = BackupWorker(settings)
    worker._state({"success_at": "2026-01-01T00:00:00+00:00", "file": old.name})

    async def create(*command, **kwargs):
        Path(command[command.index("--file") + 1]).write_bytes(b"partial")
        return Process(1)

    monkeypatch.setattr("app.services.maintenance.shutil.which", lambda name: name)
    monkeypatch.setattr("app.services.maintenance.asyncio.create_subprocess_exec", create)
    with pytest.raises(RuntimeError):
        await worker.create_backup()
    assert old.read_bytes() == b"old data"
    assert worker.state()["success_at"] == "2026-01-01T00:00:00+00:00"
    assert "error" in worker.state() and not list(settings.backups_dir.glob("*.partial"))


def test_maintenance_file_lock_blocks_second_process_equivalent(tmp_path):
    with file_lock(tmp_path / "backup.lock"):
        with pytest.raises(ValueError):
            with file_lock(tmp_path / "backup.lock"):
                pass
    with file_lock(tmp_path / "backup.lock"):
        pass


async def test_encrypted_mirror_keeps_no_partial_plaintext(settings, monkeypatch, tmp_path):
    directory = tmp_path / "external"
    directory.mkdir()
    settings.backup_mirror_dir = directory
    settings.backup_age_recipient = "age1testpublicrecipient"
    source = settings.backups_dir / "tgviewer_test.dump"
    source.write_bytes(b"plain database dump")
    calls = []

    async def create(*command, **kwargs):
        calls.append(command)
        Path(command[command.index("--output") + 1]).write_bytes(
            b"age-encryption.org/v1" + b"x" * 200
        )
        return Process()

    monkeypatch.setattr("app.services.maintenance.shutil.which", lambda name: name)
    monkeypatch.setattr("app.services.maintenance.asyncio.create_subprocess_exec", create)
    await BackupWorker(settings).mirror(source)
    assert (directory / (source.name + ".age")).exists()
    assert len(list(directory.iterdir())) == 1
    assert "--recipient" in calls[0]
