import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

from aiogram.types import Chat, Message, User
from sqlalchemy import select

from app.bot.common import Gate
from app.bot.handlers import create_router
from app.db import Database
from app.enums import JobMode, JobStatus, OperatorRole, ResultStatus
from app.logging_setup import SecretFilter
from app.models import AccountJobResult, Job, Operator, TelegramAccount, utcnow
from app.services.accounts import AccountService
from app.services.control import aware
from app.services.maintenance import RetentionWorker
from app.services.reports import ReportService, render_workbook


def message(chat_type="private", user_id=1, chat_id=1, text="/menu"):
    return Message(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="User"),
        text=text,
    )


async def test_group_messages_denied_even_for_owner(settings, db):
    handler = AsyncMock()
    gate = Gate(SimpleNamespace(settings=settings, db=db))
    await gate(handler, message("supergroup", 1, -10), {})
    handler.assert_not_called()


async def test_former_owner_role_is_not_authorization(settings, db):
    async with db.session() as session:
        session.add(Operator(telegram_id=2, role=OperatorRole.MAIN_ADMIN, active=True))
    handler = AsyncMock()
    gate = Gate(SimpleNamespace(settings=settings, db=db))
    await gate(handler, message(user_id=2, chat_id=2), {})
    handler.assert_not_called()
    await Database.sync_owner(db, 1)
    async with db.session() as session:
        assert not (await session.get(Operator, 2)).active
        assert (await session.get(Operator, 1)).active


async def test_manager_blocked_from_owner_callbacks(settings, db):
    from aiogram import Bot
    from aiogram.client.session.base import BaseSession
    from aiogram.types import CallbackQuery

    class Session(BaseSession):
        async def close(self):
            pass

        async def make_request(self, bot, method, timeout=None):
            return True

        async def stream_content(self, *args, **kwargs):
            yield b""

    bot = Bot(settings.bot_token, session=Session())
    async with db.session() as session:
        session.add(Operator(telegram_id=2, role=OperatorRole.MANAGER, active=True))
    msg = message(user_id=2, chat_id=2).as_(bot)
    event = CallbackQuery(
        id="1", from_user=msg.from_user, chat_instance="x", data="own:add", message=msg
    ).as_(bot)
    handler = AsyncMock()
    gate = Gate(SimpleNamespace(settings=settings, db=db))
    await gate(handler, event, {})
    handler.assert_not_called()


def test_formula_payload_is_literal_and_report_permissions(tmp_path):
    path = tmp_path / "report.xlsx"
    render_workbook(
        path,
        [
            (
                "Test",
                [
                    ["Title"],
                    ['=HYPERLINK("https://example.invalid","test")'],
                    ["+1+1"],
                    ["@SUM(1)"],
                ],
            )
        ],
    )
    with ZipFile(path) as z:
        xml = z.read("xl/worksheets/sheet1.xml").decode()
        assert "<f>" not in xml and "HYPERLINK" in xml
    assert path.stat().st_mode & 0o777 == 0o600


async def test_archiving_keeps_result_identity_and_removes_key(db, settings, seeded):
    async with db.session() as session:
        job = Job(
            mode=JobMode.MANUAL_FULL, status=JobStatus.COMPLETED, created_by=1, expected_results=1
        )
        session.add(job)
        await session.flush()
        session.add(
            AccountJobResult(
                job_id=job.id,
                account_id=seeded.accounts[0].id,
                channel_id=seeded.channels[0].id,
                account_number=1,
                account_phone="+79990000001",
                channel_title="original title",
                channel_url="https://t.me/original",
                status=ResultStatus.COMPLETED,
                state="done",
            )
        )
    runtime = SimpleNamespace(cancel_account=AsyncMock())
    await AccountService(settings, db, seeded.box, runtime).delete(seeded.accounts[0].id, 1)
    async with db.session() as session:
        row = await session.get(TelegramAccount, seeded.accounts[0].id)
        result = await session.scalar(select(AccountJobResult))
    assert row.encrypted_session == "" and row.archived_at
    assert result.account_phone == "+79990000001" and result.channel_title == "original title"
    assert (await ReportService(settings, db).build_job_report(job.id)).exists()


async def test_retention_extension_never_changes_finish_time(db, settings):
    finished = utcnow() - timedelta(days=10)
    async with db.session() as session:
        job = Job(
            mode=JobMode.MANUAL_FULL,
            status=JobStatus.COMPLETED,
            created_by=1,
            finished_at=finished,
            retention_until=finished + timedelta(days=30),
        )
        session.add(job)
    service = RetentionWorker(settings, db, None)
    await service.keep(job.id, 1)
    await service.delete_confirmed(job.id, 1)
    async with db.session() as session:
        row = await session.get(Job, job.id)
    assert row and aware(row.finished_at) == finished
    assert aware(row.retention_until) == finished + timedelta(days=60)
    async with db.session() as session:
        (await session.get(Job, job.id)).retention_until = utcnow() - timedelta(seconds=1)
    await service.cleanup()
    async with db.session() as session:
        assert await session.get(Job, job.id) is None


def test_logs_redact_credentials_and_exception_payload(settings):
    record = logging.LogRecord(
        "x",
        logging.ERROR,
        "",
        0,
        "error %s",
        (settings.bot_token + " https://login:password@host +79991234567",),
        (ValueError, ValueError("secret password"), None),
    )
    SecretFilter(settings).filter(record)
    result = record.getMessage()
    assert (
        settings.bot_token not in result
        and "password@" not in result
        and "+79991234567" not in result
        and record.exc_info is None
    )


def test_router_constructs_without_legacy_services():
    router = create_router(SimpleNamespace())
    assert len(router.sub_routers) == 5
