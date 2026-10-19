from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import select

from app.bot.common import BotServices
from app.bot.handlers import create_router
from app.enums import OperatorRole
from app.models import ImportBatch, Operator
from app.services.onboarding import OnboardingService


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if type(method).__name__ in (
            "AnswerCallbackQuery",
            "DeleteMessage",
            "EditMessageReplyMarkup",
        ):
            return True
        return Message(
            message_id=len(self.calls) + 10,
            date=datetime.now(),
            chat=Chat(id=int(getattr(method, "chat_id", 1)), type="private"),
            from_user=User(id=bot.id, is_bot=True, first_name="Bot"),
            text=getattr(method, "text", None),
            reply_markup=getattr(method, "reply_markup", None),
        ).as_(bot)

    async def stream_content(self, *args, **kwargs):
        yield b""


@pytest.fixture
async def bot_ui(settings, db):
    session = FakeSession()
    bot = Bot(settings.bot_token, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    onboarding = OnboardingService(settings, db, None, None, bot)
    services = BotServices(
        settings,
        db,
        SimpleNamespace(_tasks={}),
        SimpleNamespace(activity={}),
        None,
        None,
        None,
        None,
        None,
        None,
        onboarding,
        None,
    )
    dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dispatcher.include_router(create_router(services))
    sequence = 0

    async def send(text=None, callback=None, user=1, chat_type="private", message_id=None):
        nonlocal sequence
        sequence += 1
        actor = User(id=user, is_bot=False, first_name="User")
        message = Message(
            message_id=message_id or sequence,
            date=datetime.now(),
            chat=Chat(id=user if chat_type == "private" else -1, type=chat_type),
            from_user=actor,
            text=text or "Screen",
        ).as_(bot)
        if callback:
            query = CallbackQuery(
                id=str(sequence), from_user=actor, chat_instance="x", message=message, data=callback
            ).as_(bot)
            update = Update(update_id=sequence, callback_query=query)
        else:
            update = Update(update_id=sequence, message=message)
        await dispatcher.feed_update(bot, update)

    yield SimpleNamespace(
        send=send, calls=session.calls, services=services, dispatcher=dispatcher, bot=bot
    )
    await onboarding.shutdown()
    await dispatcher.storage.close()
    await bot.session.close()


async def test_full_phone_list_preview_confirmation_and_results(bot_ui, db, seeded):
    ui = bot_ui
    await ui.send("/start")
    await ui.send(callback="own:add")
    await ui.send(callback="own:add:phones")
    await ui.send("+79991234567\n+79991234568\n+79991234567")
    pending = ui.services.onboarding.pending
    assert pending and len(pending.items) == 2
    assert any(type(call).__name__ == "DeleteMessage" for call in ui.calls)
    async with db.session() as session:
        assert await session.scalar(select(ImportBatch)) is None
    ui.services.onboarding._qr = AsyncMock(
        side_effect=[(seeded.accounts[0], True), (seeded.accounts[1], True)]
    )
    await ui.send(callback="own:import:start:" + pending.token)
    await ui.services.onboarding.active.task
    async with db.session() as session:
        row = await session.scalar(select(ImportBatch))
    assert row.status == "completed" and len(row.results) == 2
    await ui.send(callback="import:result:" + row.id)
    assert any("Новых: 2" in (getattr(call, "text", "") or "") for call in ui.calls)
    assert any("Записей: 2/2" in (getattr(call, "text", "") or "") for call in ui.calls)


async def test_old_callback_and_group_do_not_trigger_mutation(bot_ui):
    ui = bot_ui
    await ui.send(callback="own:import:start:expired")
    assert ui.services.onboarding.active is None
    count = len(ui.calls)
    await ui.send("/start", chat_type="supergroup")
    assert len(ui.calls) == count


async def test_phone_code_menu_preview_and_selected_method(bot_ui, db, seeded):
    ui = bot_ui
    await ui.send(callback="own:add")
    menu = ui.calls[-1].reply_markup
    callbacks = [b.callback_data for row in menu.inline_keyboard for b in row]
    assert "own:add:code" in callbacks and "own:add:qr" in callbacks
    await ui.send(callback="own:add:code")
    await ui.send("+79991234567")
    pending = ui.services.onboarding.pending
    assert pending and len(pending.items) == 1
    ui.services.onboarding._phone = AsyncMock(return_value=(seeded.accounts[0], True))
    ui.services.onboarding._qr = AsyncMock(side_effect=AssertionError("QR must not run"))
    await ui.send(callback="own:import:startcode:" + pending.token)
    await ui.services.onboarding.active.task
    ui.services.onboarding._phone.assert_awaited_once()
    ui.services.onboarding._qr.assert_not_awaited()
    async with db.session() as session:
        row = await session.scalar(select(ImportBatch))
    assert row.status == "completed" and row.results[0]["status"] == "added"


async def test_real_keypad_callback_router_and_secret_message_gate(bot_ui, db):
    import asyncio

    from app.services.onboarding import ActiveImport, CodeInput

    ui = bot_ui
    active = ActiveImport("token", "batch", 1, login_method="code")
    future = asyncio.get_running_loop().create_future()
    active.code = CodeInput("key-token", future, 5, 0, False, digits="5729", message_id=100)
    ui.services.onboarding.active = active
    await ui.send("12345")
    assert not future.done() and active.code.digits == "5729"
    assert any(type(call).__name__ == "DeleteMessage" for call in ui.calls)
    await ui.send(callback="login:code:key-token:4", message_id=100)
    await ui.send(callback="login:code:key-token:ok", message_id=100)
    assert future.result() == "57294"
    assert active.code.digits == ""


async def test_keypad_cannot_be_used_by_manager(bot_ui, db):
    import asyncio

    from app.services.onboarding import ActiveImport, CodeInput

    async with db.session() as session:
        session.add(Operator(telegram_id=2, role=OperatorRole.MANAGER, active=True))
    ui = bot_ui
    active = ActiveImport("token", "batch", 1, login_method="code")
    future = asyncio.get_running_loop().create_future()
    active.code = CodeInput("key-token", future, 5, 0, False, message_id=100)
    ui.services.onboarding.active = active
    await ui.send(callback="login:code:key-token:4", message_id=100, user=2)
    assert not future.done() and active.code.digits == ""
    future.cancel()


async def test_manager_reads_accounts_but_cannot_import(bot_ui, db, seeded):
    async with db.session() as session:
        session.add(Operator(telegram_id=2, role=OperatorRole.MANAGER, active=True))
    ui = bot_ui
    await ui.send(callback="accounts:all:0", user=2)
    texts = [getattr(call, "text", "") or "" for call in ui.calls]
    assert any("Аккаунты" in text for text in texts)
    await ui.send(callback="own:add:qr", user=2)
    assert ui.services.onboarding.pending is None
    assert any("только главному" in (getattr(call, "text", "") or "") for call in ui.calls)


async def test_restricted_manager_cannot_see_other_channels_or_use_old_button(bot_ui, db, seeded):
    from app.services.control import put_setting

    async with db.session() as session:
        session.add(Operator(telegram_id=2, role=OperatorRole.MANAGER, active=True))
    await put_setting(db, "manager_channels:2", {"ids": [seeded.channels[0].id]})
    ui = bot_ui
    await ui.send(callback="channels:0", user=2)
    menu = next(
        call.reply_markup for call in reversed(ui.calls) if getattr(call, "reply_markup", None)
    )
    callbacks = [button.callback_data for row in menu.inline_keyboard for button in row]
    assert f"channel:{seeded.channels[0].id}" in callbacks
    assert f"channel:{seeded.channels[1].id}" not in callbacks
    await ui.send(callback=f"channel:{seeded.channels[1].id}", user=2)
    assert "не включён в доступ" in ui.calls[-1].text
