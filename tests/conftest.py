from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.enums import AccountStatus
from app.models import Base, Channel, TelegramAccount
from app.security import SecretBox
from app.services.control import put_setting


@pytest.fixture
def settings(tmp_path):
    value = Settings(
        _env_file=None,
        bot_token="123456789:" + ("A" * 35),
        main_admin_id=1,
        api_id=123,
        api_hash="a" * 32,
        database_url="postgresql+asyncpg://unused:unused@127.0.0.1/unused",
        session_encryption_key=Fernet.generate_key().decode(),
        proxy_policy="allow_direct",
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        backups_dir=tmp_path / "backups",
        logs_dir=tmp_path / "logs",
    )
    value.ensure_directories()
    return value


class TestDB:
    __test__ = False

    def __init__(self, engine):
        self.engine = engine
        self.session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self):
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise


@pytest.fixture
async def db(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "test.db"))

    @event.listens_for(engine.sync_engine, "connect")
    def pragmas(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = TestDB(engine)
    await put_setting(db, "global_pause", {"paused": False})
    yield db
    await engine.dispose()


@pytest.fixture
async def seeded(db, settings):
    box = SecretBox(settings.session_encryption_key)
    async with db.session() as session:
        accounts = [
            TelegramAccount(
                display_number=i,
                telegram_user_id=100 + i,
                phone="+7999000000" + str(i),
                encrypted_session=box.encrypt("fake"),
                status=AccountStatus.ACTIVE,
            )
            for i in (1, 2)
        ]
        channels = [
            Channel(
                username="channel" + str(i),
                title="Channel " + str(i),
                url="https://t.me/channel" + str(i),
                created_by=1,
            )
            for i in (1, 2)
        ]
        session.add_all(accounts + channels)
    return SimpleNamespace(accounts=accounts, channels=channels, box=box)
