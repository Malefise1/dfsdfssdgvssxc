import asyncio
import io
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from app.services.accounts import AccountService, fingerprint
from app.services.imports import ImportItem, parse_upload, session_from_sqlite
from app.services.onboarding import ActiveImport, OnboardingService


def string_session(key=b"a" * 256):
    row = StringSession()
    row.set_dc(2, "149.154.167.51", 443)
    row.auth_key = AuthKey(key)
    return row.save()


def sqlite_bytes(tmp_path):
    path = tmp_path / "fixture.session"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE sessions(dc_id INTEGER,server_address TEXT,port INTEGER,auth_key BLOB)"
    )
    con.execute("INSERT INTO sessions VALUES (?,?,?,?)", (2, "149.154.167.51", 443, b"a" * 256))
    con.commit()
    con.close()
    return path.read_bytes()


def zip_bytes(entries):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as z:
        for name, value in entries:
            z.writestr(name, value)
    return buffer.getvalue()


def test_json_txt_and_sqlite_deduplicated(settings, tmp_path):
    session = string_session()
    data = zip_bytes(
        [
            ("a.session", sqlite_bytes(tmp_path)),
            ("b.json", json.dumps({"accounts": [{"session": session}, {"phone": "+79991234567"}]})),
        ]
    )
    rows = parse_upload("accounts.zip", data, settings)
    assert len(rows) == 2 and [r.kind for r in rows] == ["session", "phone"]
    assert session not in repr(rows)
    assert fingerprint(rows[0].value) == fingerprint(session)


def test_tpanel_sidecars_with_empty_sessions_become_qr_phones(settings):
    data = zip_bytes(
        [
            (
                "6285187744852.json",
                json.dumps(
                    {
                        "session_file": "6285187744852",
                        "phone": "6285187744852",
                        "app_id": 2040,
                        "app_hash": "b" * 32,
                        "program": "tpanel.online",
                    }
                ),
            ),
            ("6285187744852.session", b""),
            (
                "959776515307.json",
                json.dumps(
                    {
                        "session_file": "959776515307",
                        "phone": "+959776515307",
                        "app_id": 2040,
                        "app_hash": "c" * 32,
                    }
                ),
            ),
            ("959776515307.session", b""),
        ]
    )
    rows = parse_upload("tpanel.zip", data, settings)
    assert [(row.kind, row.value, row.note) for row in rows] == [
        ("phone", "+6285187744852", "empty_session"),
        ("phone", "+959776515307", "empty_session"),
    ]


def test_tpanel_sidecar_with_real_session_imports_once(settings, tmp_path):
    data = zip_bytes(
        [
            (
                "79991234567.json",
                json.dumps({"session_file": "79991234567", "phone": "79991234567"}),
            ),
            ("79991234567.session", sqlite_bytes(tmp_path)),
        ]
    )
    rows = parse_upload("tpanel.zip", data, settings)
    assert len(rows) == 1
    assert rows[0].kind == "session" and rows[0].note == ""


def test_pair_archive_limit_counts_accounts_not_sidecar_files(settings):
    settings.max_import_accounts = 2
    data = zip_bytes(
        [
            ("one.json", json.dumps({"session_file": "one", "phone": "79991234561"})),
            ("one.session", b""),
            ("two.json", json.dumps({"session_file": "two", "phone": "79991234562"})),
            ("two.session", b""),
        ]
    )
    rows = parse_upload("pairs.zip", data, settings)
    assert [row.value for row in rows] == ["+79991234561", "+79991234562"]


def test_orphan_empty_session_is_rejected(settings):
    with pytest.raises(ValueError, match="пустая .session"):
        parse_upload("input.zip", zip_bytes([("orphan.session", b"")]), settings)


@pytest.mark.parametrize(
    "name", ["../escape.session", "/absolute.session", "a/../../b.txt", "C:/file.txt", "a.exe"]
)
def test_unsafe_archive_rejected(name, settings):
    with pytest.raises(ValueError):
        parse_upload("input.zip", zip_bytes([(name, b"+79991234567")]), settings)


def test_import_limits_before_connection(settings):
    settings.max_import_accounts = 2
    with pytest.raises(ValueError):
        parse_upload("phones.txt", b"+79991234561\n+79991234562\n+79991234563", settings)
    with pytest.raises(ValueError):
        parse_upload("file.txt", b"x" * (settings.max_upload_bytes + 1), settings)


def test_sqlite_readonly_parser_rejects_missing_auth_key(settings, tmp_path):
    con = sqlite3.connect(tmp_path / "bad.session")
    con.execute("CREATE TABLE sessions(dc_id,server_address,port,auth_key)")
    con.execute("INSERT INTO sessions VALUES (2,'127.0.0.1',443,NULL)")
    con.commit()
    con.close()
    with pytest.raises(ValueError):
        session_from_sqlite((tmp_path / "bad.session").read_bytes())


async def test_existing_session_does_not_open_second_connection(settings, db, seeded, monkeypatch):
    value = string_session()
    from app.models import TelegramAccount

    async with db.session() as session:
        row = await session.get(TelegramAccount, seeded.accounts[0].id)
        row.session_fingerprint = fingerprint(value)
    build = AsyncMock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr("app.services.accounts.build_user_client", build)
    service = AccountService(settings, db, seeded.box, SimpleNamespace())
    row, added = await service.import_session(value, 1)
    assert not added and row.id == seeded.accounts[0].id
    build.assert_not_called()


async def test_preview_expires_and_only_owner_can_start(settings, db):
    service = OnboardingService(settings, db, None, None, None)
    token = service.prepare([ImportItem("one", "phone", "+79991234567")])
    with pytest.raises(ValueError):
        await service.start(token, 2)
    service.pending.expires = time.monotonic() - 1
    with pytest.raises(ValueError):
        await service.start(token, 1)
    assert service.pending is None


async def test_2fa_not_accepted_when_deletion_fails(settings, db):
    service = OnboardingService(settings, db, None, None, None)
    service.active = ActiveImport("token", "batch", 1)
    future = asyncio.get_running_loop().create_future()
    service.active.password = future
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        text="secret",
        delete=AsyncMock(side_effect=RuntimeError()),
        answer=AsyncMock(),
    )
    assert await service.submit_password(message)
    assert not future.done()
    message.delete = AsyncMock()
    assert await service.submit_password(message)
    assert future.result() == "secret"


async def test_batch_partial_failure_reports_every_item_and_wipes_material(settings, db):
    accounts = SimpleNamespace(
        import_session=AsyncMock(
            side_effect=[(SimpleNamespace(display_number=3), True), ValueError("bad session")]
        )
    )
    message = SimpleNamespace(edit_text=AsyncMock(), edit_reply_markup=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock(return_value=message))
    service = OnboardingService(settings, db, accounts, None, bot)
    items = [ImportItem("a", "session", "first"), ImportItem("b", "session", "second")]
    token = service.prepare(items)
    id_ = await service.start(token, 1)
    await service.active.task
    from app.models import ImportBatch

    async with db.session() as session:
        row = await session.get(ImportBatch, id_)
    assert len(row.results) == 2 and [r["status"] for r in row.results] == ["added", "failed"]
    assert all(item.value == "" for item in items)
    assert "first" not in json.dumps(row.results)


async def test_qr_2fa_success_cleans_qr_and_stores_only_verified_session(settings, db, monkeypatch):
    from telethon.errors import SessionPasswordNeededError
    from telethon.tl.types import User

    user = User(id=99, first_name="Owner", phone="79991234567")
    qr = SimpleNamespace(
        url="tg://login?token=test", wait=AsyncMock(side_effect=SessionPasswordNeededError(None))
    )
    client = SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        qr_login=AsyncMock(return_value=qr),
        sign_in=AsyncMock(return_value=user),
        is_user_authorized=AsyncMock(return_value=True),
        log_out=AsyncMock(),
        session=SimpleNamespace(save=lambda: "verified-session"),
    )
    monkeypatch.setattr("app.services.onboarding.build_user_client", lambda *a, **k: client)
    image = SimpleNamespace(delete=AsyncMock())
    prompt = SimpleNamespace(delete=AsyncMock())
    bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=image), send_message=AsyncMock(return_value=prompt)
    )
    accounts = SimpleNamespace(
        store_verified=AsyncMock(return_value=(SimpleNamespace(display_number=1), True))
    )
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def bootstrap(cancel=None):
        yield

    runtime = SimpleNamespace(proxy_for=AsyncMock(return_value=None), bootstrap=bootstrap)
    service = OnboardingService(settings, db, accounts, runtime, bot)
    active = ActiveImport("token", "batch", 1)
    service.active = active
    task = asyncio.create_task(
        service._qr(active, ImportItem("one", "phone", "+79991234567"), 1, 1, None)
    )
    for _ in range(100):
        if active.password:
            break
        await asyncio.sleep(0.01)
    secret_message = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        text="test-password",
        delete=AsyncMock(),
        answer=AsyncMock(),
    )
    assert await service.submit_password(secret_message)
    row, added = await asyncio.wait_for(task, 2)
    assert added
    secret_message.delete.assert_awaited_once()
    client.sign_in.assert_awaited_once_with(password="test-password")
    client.log_out.assert_not_called()
    image.delete.assert_awaited_once()
    client.disconnect.assert_awaited_once()
    assert active.password is None
    assert accounts.store_verified.call_args.args[0] == "verified-session"
