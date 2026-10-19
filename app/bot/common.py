from __future__ import annotations

import asyncio
import io
import logging
import time
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass, field
from html import escape
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.emoji import button
from app.enums import OperatorRole
from app.errors import safe_error
from app.models import Operator
from app.repositories import touch_known_user
from app.services.control import aware, setting
from app.services.reports import masked_phone

logger = logging.getLogger(__name__)
PAGE = 8


class Input(StatesGroup):
    text = State()
    file = State()
    selection = State()
    confirmation = State()


@dataclass
class BotServices:
    settings: Any
    db: Any
    runner: Any
    runtime: Any
    accounts: Any
    proxies: Any
    reports: Any
    retention: Any
    subscriptions: Any
    schedules: Any
    onboarding: Any
    backups: Any
    health: dict = field(default_factory=dict)
    background: set = field(default_factory=set)

    def spawn(self, coroutine, message):
        if len(self.background) >= 12:
            coroutine.close()
            raise ValueError("Слишком много фоновых операций. Дождитесь результата.")

        async def run():
            try:
                await coroutine
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Background operation: %s", type(exc).__name__)
                with suppress(Exception):
                    await message.answer(escape(safe_error(exc)))

        task = asyncio.create_task(run())
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task


def owner(s, user_id):
    return user_id == s.settings.main_admin_id


def require_owner(s, user_id):
    if not owner(s, user_id):
        raise ValueError("Это действие доступно только главному администратору")


def kb(*rows):
    buttons = []
    for row in rows:
        if row:
            buttons.append(
                [
                    button(
                        item[0],
                        item[1],
                        item[2] if len(item) > 2 else 83,
                        style=item[3] if len(item) > 3 else None,
                    )
                    for item in row
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def home_row():
    return [("Меню", "menu:home", 43)]


def pages(prefix, page, has_next):
    row = []
    if page:
        row.append(("←", f"{prefix}:{page - 1}", 67))
    if has_next:
        row.append(("→", f"{prefix}:{page + 1}", 39))
    return row


async def show(event, text, keyboard=None):
    if isinstance(event, CallbackQuery):
        if event.message and event.message.text:
            try:
                await event.message.edit_text(text, reply_markup=keyboard)
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in exc.message:
                    return
        if event.message:
            await event.message.answer(text, reply_markup=keyboard)
    else:
        await event.answer(text, reply_markup=keyboard)


def date(s, value):
    return aware(value).astimezone(s.settings.tz).strftime("%d.%m.%Y %H:%M") if value else "—"


async def still_authorized(s, actor):
    if owner(s, actor):
        return True
    async with s.db.session() as session:
        row = await session.get(Operator, actor)
        return bool(row and row.active and row.role == OperatorRole.MANAGER)


async def hide_phones(s, actor):
    policy = await setting(s.db, "manager_show_phones", {"value": s.settings.manager_show_phones})
    return not owner(s, actor) and not policy["value"]


async def phone(s, actor, value):
    return masked_phone(value) if await hide_phones(s, actor) else value


async def download_secret(message, settings):
    document = message.document
    if (
        not document
        or document.file_size is None
        or not 0 < document.file_size <= settings.max_upload_bytes
    ):
        raise ValueError(
            f"Пришлите файл размером не более {settings.max_upload_bytes // 1_000_000} МБ"
        )
    # Resolve location before deleting; the raw credential file is never written to the work tree.
    remote = await message.bot.get_file(document.file_id)
    try:
        await message.delete()
    except Exception as exc:
        raise ValueError(
            "Не удалось удалить сообщение с файлом. Импорт не начат. Удалите сообщение вручную и повторите."
        ) from exc

    class LimitedBuffer(io.BytesIO):
        def write(self, chunk):
            if self.tell() + len(chunk) > settings.max_upload_bytes:
                raise ValueError("Файл слишком большой")
            return super().write(chunk)

    stream = LimitedBuffer()
    await message.bot.download_file(remote.file_path, destination=stream, timeout=60)
    if stream.tell() > settings.max_upload_bytes:
        raise ValueError("Файл слишком большой")
    return document.file_name or "accounts.txt", stream.getvalue()


class Gate(BaseMiddleware):
    def __init__(self, services):
        self.s = services
        self.rates = OrderedDict()

    def allow_rate(self, user_id):
        now = time.monotonic()
        tokens, updated = self.rates.pop(user_id, (12.0, now))
        tokens = min(12, tokens + (now - updated) * 3)
        self.rates[user_id] = (max(0, tokens - 1), now)
        if len(self.rates) > 2048:
            self.rates.popitem(last=False)
        return tokens >= 1

    async def __call__(self, handler, event, data):
        callback = isinstance(event, CallbackQuery)
        message = event.message if callback else event
        user = event.from_user
        if not user or not isinstance(message, Message):
            return
        if message.chat.type != "private" or message.chat.id != user.id:
            if callback:
                with suppress(Exception):
                    await event.answer("Откройте личный чат с ботом", show_alert=True)
            return
        if not self.allow_rate(user.id):
            if callback:
                with suppress(Exception):
                    await event.answer("Слишком часто. Подождите несколько секунд.")
            return
        async with self.s.db.session() as session:
            operator = await session.get(Operator, user.id)
            permitted = owner(self.s, user.id) or bool(
                operator and operator.active and operator.role == OperatorRole.MANAGER
            )
            if not callback and (message.text or "").split(" ", 1)[0].split("@", 1)[0] == "/start":
                await touch_known_user(session, user.id, user.username, user.full_name)
        if not permitted:
            if callback:
                await event.answer("Нет доступа", show_alert=True)
            elif (message.text or "").startswith("/start"):
                await message.answer(
                    f"Ваш ID: <code>{user.id}</code>. Главный администратор может добавить вас в разделе «Управляющие»."
                )
            return
        if callback:
            with suppress(Exception):
                await event.answer()
        try:
            if callback and (event.data or "").startswith("own:"):
                require_owner(self.s, user.id)
            if not callback and message.text and not message.text.startswith("/"):
                if await self.s.onboarding.reject_code_message(message):
                    return
                if await self.s.onboarding.submit_password(message):
                    return
            return await handler(event, data)
        except (ValueError, LookupError) as exc:
            await message.answer(escape(str(exc)[:1000]), reply_markup=kb(home_row()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Bot handler: %s", type(exc).__name__)
            await message.answer(escape(safe_error(exc)), reply_markup=kb(home_row()))
