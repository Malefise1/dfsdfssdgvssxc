"""Runs on PostgreSQL; each test creates and drops only its own random schema.
Set TGVIEWER_TEST_DATABASE_URL to a disposable test database. Never use production.
"""

import json
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.migrations import migrate

URL = os.environ.get("TGVIEWER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not URL, reason="Set TGVIEWER_TEST_DATABASE_URL for PostgreSQL migration checks"
)


@pytest.fixture
async def pg():
    name = "tgviewer_test_" + uuid.uuid4().hex
    engine = create_async_engine(
        URL,
        pool_size=1,
        max_overflow=0,
        connect_args={"ssl": False} if os.environ.get("TGVIEWER_TEST_NO_SSL") else {},
    )
    async with engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA " + name))
        await conn.execute(text("SET search_path TO " + name))
    try:
        yield engine
    finally:
        if os.environ.get("TGVIEWER_TEST_PGLITE"):
            # The WASM socket multiplexer does not reliably complete close/DDL cleanup.
            # This option is only for an ephemeral PGlite process discarded after the test.
            await engine.dispose(close=False)
        else:
            async with engine.begin() as conn:
                await conn.execute(text("DROP SCHEMA " + name + " CASCADE"))
            await engine.dispose()


async def test_fresh_schema_migrates_and_rerun_preserves_pause(pg):
    assert await migrate(pg)
    async with pg.begin() as conn:
        assert await conn.scalar(text("SELECT max(version) FROM schema_migrations")) == 2
        assert (
            await conn.scalar(text("SELECT value FROM system_settings WHERE key='global_pause'"))
        )["paused"]
        await conn.execute(
            text("UPDATE system_settings SET value=CAST(:value AS JSON) WHERE key='global_pause'"),
            {"value": '{"paused":false}'},
        )
    assert not await migrate(pg)
    async with pg.connect() as conn:
        assert not (
            await conn.scalar(text("SELECT value FROM system_settings WHERE key='global_pause'"))
        )["paused"]


async def test_v1_data_preserved_and_legacy_running_cancelled(pg):
    statements = json.loads((Path(__file__).parent / "fixtures/schema_v1.json").read_text())
    async with pg.begin() as conn:
        for sql in statements:
            await conn.execute(text(sql))
        await conn.execute(
            text(
                "INSERT INTO telegram_accounts(id,display_number,telegram_user_id,phone,encrypted_session,status,error_streak,created_at,updated_at) VALUES (1,7,123,'+79991234567','encrypted-old','ACTIVE',0,now(),now())"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO channels(id,username,title,url,permanent,active,check_interval_seconds,created_by,created_at) VALUES(1,'channel','Title','https://t.me/channel',true,true,300,1,now())"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO jobs(id,mode,status,created_by,created_at) VALUES('legacy','MANUAL_FULL','RUNNING',1,now())"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO account_job_results(job_id,account_id,channel_id,status,available_posts,viewed_posts,last_message_id,started_at) VALUES('legacy',1,1,'PARTIAL',3,2,10,now())"
            )
        )
    assert await migrate(pg)
    async with pg.connect() as conn:
        row = (await conn.execute(text("SELECT * FROM telegram_accounts"))).mappings().one()
        assert row["display_number"] == 7 and row["encrypted_session"] == "encrypted-old"
        job = (await conn.execute(text("SELECT * FROM jobs"))).mappings().one()
        assert (
            job["status"] == "CANCELLED"
            and job["cancel_requested"]
            and job["stop_reason"] == "legacy_migration"
        )
        assert job["summary"]["legacy_planning_unknown"]
        result = (await conn.execute(text("SELECT * FROM account_job_results"))).mappings().one()
        assert (
            result["account_number"] == 7
            and result["account_phone"] == "+79991234567"
            and result["channel_url"] == "https://t.me/channel"
        )
    assert not await migrate(pg)
