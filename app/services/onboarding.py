"""Account onboarding. Authentication material lives only in bounded, expiring memory."""

from __future__ import annotations

import asyncio
import io
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass, field
from html import escape

import qrcode
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, update
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from app.enums import AccountStatus
from app.errors import UserInputError, safe_error
from app.models import ImportBatch, TelegramAccount
from app.services.accounts import normalize_phone
from app.services.control import audit
from app.services.imports import ImportItem
from app.services.runtime import cancellable
from app.telegram.client import build_user_client


@dataclass
class PendingImport:
    token: str
    items: list[ImportItem] = field(repr=False)
    expires: float = 0
    reauth_id: int | None = None


@dataclass
class ActiveImport:
    token: str
    batch_id: str
    actor: int
    current: asyncio.Event = field(default_factory=asyncio.Event)
    stopping: bool = False
    reauth_id: int | None = None
    password: asyncio.Future | None = field(default=None, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    login_method: str = "qr"
    code: CodeInput | None = field(default=None, repr=False)


@dataclass
class CodeInput:
    token: str
    result: asyncio.Future = field(repr=False)
    length: int
    resend_at: float
    can_resend: bool
    digits: str = field(default="", repr=False)
    message_id: int | None = None


class OnboardingService:
    def __init__(self, settings, db, accounts, runtime, bot):
        self.settings, self.db, self.accounts, self.runtime, self.bot = (
            settings,
            db,
            accounts,
            runtime,
            bot,
        )
        self.pending: PendingImport | None = None
        self.active: ActiveImport | None = None
        self._lock = asyncio.Lock()

    async def recover(self):
        async with self.db.session() as session:
            await session.execute(
                update(ImportBatch)
                .where(ImportBatch.status.in_(["pending", "running"]))
                .values(status="interrupted")
            )

    def prepare(self, items, reauth_id=None):
        if self.active:
            raise UserInputError(
                "Уже идёт добавление аккаунтов. Дождитесь окончания или остановите импорт."
            )
        if not 1 <= len(items) <= self.settings.max_import_accounts:
            raise UserInputError("Слишком много аккаунтов")
        token = secrets.token_hex(8)
        self.pending = PendingImport(
            token, items, time.monotonic() + self.settings.login_ttl_seconds, reauth_id
        )
        return token

    def expire(self):
        if self.pending and self.pending.expires < time.monotonic():
            self.pending = None

    async def start(self, token, actor, login_method="qr"):
        if actor != self.settings.main_admin_id:
            raise UserInputError("Добавлять аккаунты может только главный администратор")
        if login_method not in ("qr", "code"):
            raise UserInputError("Неизвестный способ входа")
        async with self._lock:
            self.expire()
            if self.active or not self.pending or self.pending.token != token:
                raise UserInputError("Предпросмотр устарел или импорт уже запущен")
            if login_method == "code" and any(item.kind == "qr" for item in self.pending.items):
                raise UserInputError("Для входа по коду сначала укажите номер телефона")
            pending, self.pending = self.pending, None
            async with self.db.session() as session:
                batch = ImportBatch(created_by=actor, total=len(pending.items), status="running")
                session.add(batch)
                await session.flush()
            active = ActiveImport(
                token, batch.id, actor, reauth_id=pending.reauth_id, login_method=login_method
            )
            self.active = active
            active.task = asyncio.create_task(self._run(active, pending), name=f"import:{batch.id}")
            return batch.id

    def cancel(self, actor, token=None, *, skip=False):
        if actor != self.settings.main_admin_id:
            return False
        if self.pending and (token is None or self.pending.token == token):
            self.pending = None
        active = self.active
        if not active or (token is not None and active.token != token):
            return False
        active.stopping = not skip
        active.current.set()
        return True

    async def reject_code_message(self, message):
        """Codes sent as chat messages are invalidated by Telegram; use the keypad."""
        active = self.active
        if not active or message.from_user.id != active.actor or active.code is None:
            return False
        deleted = False
        with suppress(Exception):
            await message.delete()
            deleted = True
        await message.answer(
            ("" if deleted else "Удалите сообщение вручную. ")
            + "Код вводится цифровыми кнопками под запросом входа. "
            "Telegram аннулирует коды, отправленные сообщением в чат. "
            "Если отправили настоящий код, запросите новый кнопкой под запросом входа.",
            protect_content=True,
        )
        return True

    def code_keyboard(self, active, code):
        def button(text, key):
            return InlineKeyboardButton(text=text, callback_data=f"login:code:{code.token}:{key}")

        rows = [[button(str(n), str(n)) for n in range(start, start + 3)] for start in (1, 4, 7)]
        rows.append([button("Стереть", "clear"), button("0", "0"), button("⌫", "back")])
        rows.append([button(f"Войти · {len(code.digits)}/{code.length}", "ok")])
        if code.can_resend:
            rows.append([button("Запросить новый код", "resend")])
        return InlineKeyboardMarkup(inline_keyboard=rows + self.keyboard(active).inline_keyboard)

    async def code_button(self, callback):
        active = self.active
        parts = (callback.data or "").split(":")
        if (
            len(parts) != 4
            or not active
            or callback.from_user.id != active.actor
            or callback.message.chat.type != "private"
            or callback.message.chat.id != active.actor
            or active.code is None
            or active.code.token != parts[2]
            or active.code.message_id != callback.message.message_id
            or active.code.result.done()
            or active.current.is_set()
        ):
            raise UserInputError("Запрос кода устарел. Используйте кнопки текущего запроса.")
        code, key = active.code, parts[3]
        if len(key) == 1 and key in "0123456789":
            code.digits = (code.digits + key)[: code.length]
        elif key == "back":
            code.digits = code.digits[:-1]
        elif key == "clear":
            code.digits = ""
        elif key == "ok":
            if len(code.digits) != code.length:
                raise UserInputError(f"Введите {code.length} цифр кнопками, затем нажмите «Войти».")
            code.result.set_result(code.digits)
            code.digits = ""
            return
        elif key == "resend":
            wait = max(0, int(code.resend_at - time.monotonic() + 0.999))
            if not code.can_resend:
                raise UserInputError(
                    "Telegram не предложил повторную отправку. Начните вход заново."
                )
            if wait:
                raise UserInputError(f"Повторный запрос будет доступен через {wait} сек.")
            code.result.set_result(None)
            code.digits = ""
            return
        else:
            raise UserInputError("Неизвестная кнопка ввода кода")
        with suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=self.code_keyboard(active, code))

    async def submit_password(self, message):
        active = self.active
        if (
            not active
            or message.from_user.id != active.actor
            or not active.password
            or active.password.done()
        ):
            return False
        future = active.password
        # Do not process a 2FA secret unless Telegram confirms deletion of the message.
        try:
            await message.delete()
        except Exception:
            await message.answer(
                "Не удалось удалить сообщение. Пароль не принят. Удалите его вручную и используйте импорт готовой сессии или повторите попытку."
            )
            return True
        if self.active is active and active.password is future and not future.done():
            future.set_result(message.text or "")
        return True

    def keyboard(self, active):
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Пропустить аккаунт", callback_data=f"import:skip:{active.token}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Остановить импорт", callback_data=f"import:stop:{active.token}"
                    )
                ],
            ]
        )

    async def _run(self, active, pending):
        results = []
        progress = None
        status = "completed"
        try:
            progress = await self.bot.send_message(
                active.actor,
                f"Начинаю добавление: {len(pending.items)} аккаунтов.",
                reply_markup=self.keyboard(active),
            )
            for index, item in enumerate(pending.items, 1):
                if active.stopping:
                    status = "cancelled"
                    results.extend(
                        {"index": i, "status": "cancelled", "detail": "Импорт остановлен"}
                        for i in range(index, len(pending.items) + 1)
                    )
                    break
                active.current = asyncio.Event()
                try:
                    if item.kind == "session":
                        account, added = await cancellable(
                            self.accounts.import_session(item.value, active.actor),
                            active.current,
                            self.settings.rpc_timeout_seconds + 15,
                        )
                    elif active.login_method == "code":
                        account, added = await self._phone(
                            active, item, index, len(pending.items), pending.reauth_id
                        )
                    else:
                        account, added = await self._qr(
                            active, item, index, len(pending.items), pending.reauth_id
                        )
                    results.append(
                        {
                            "index": index,
                            "status": "added" if added else "duplicate",
                            "number": account.display_number,
                            "detail": "Добавлен" if added else "Уже есть",
                        }
                    )
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    results.append({"index": index, "status": "skipped", "detail": "Пропущен"})
                except Exception as exc:
                    results.append({"index": index, "status": "failed", "detail": safe_error(exc)})
                # Release credentials progressively, even if later accounts await their owner.
                item.value = ""
                async with self.db.session() as session:
                    row = await session.get(ImportBatch, active.batch_id)
                    row.results = list(results)
                if index % 5 == 0 or index == len(pending.items) or item.kind != "session":
                    with suppress(Exception):
                        await progress.edit_text(
                            f"Добавление: {index}/{len(pending.items)}.\n"
                            f"Добавлено: {sum(r['status'] == 'added' for r in results)}; "
                            f"дубликатов: {sum(r['status'] == 'duplicate' for r in results)}; "
                            f"ошибок: {sum(r['status'] == 'failed' for r in results)}.",
                            reply_markup=self.keyboard(active),
                        )
            if active.stopping:
                status = "cancelled"
        except asyncio.CancelledError:
            status = "interrupted"
        except Exception:
            status = "interrupted"
        finally:
            for item in pending.items:
                item.value = ""
            if active.password and not active.password.done():
                active.password.cancel()
            if active.code:
                active.code.digits = ""
                if not active.code.result.done():
                    active.code.result.cancel()
                active.code = None
            if active.stopping:
                status = "cancelled"
            recorded = {r["index"] for r in results}
            for index in range(1, len(pending.items) + 1):
                if index not in recorded:
                    results.append(
                        {
                            "index": index,
                            "status": status,
                            "detail": "Не обработан: операция прервана",
                        }
                    )
            async with self.db.session() as session:
                row = await session.get(ImportBatch, active.batch_id)
                row.results, row.status = results, status
            await audit(
                self.db,
                active.actor,
                "accounts.import_finished",
                active.batch_id,
                total=len(pending.items),
                processed=len(results),
                status=status,
            )
            if self.active is active:
                self.active = None
            with suppress(Exception):
                await self.bot.send_message(
                    active.actor,
                    f"Добавление завершено: {len(results)}/{len(pending.items)}. "
                    f"Новых: {sum(r['status'] == 'added' for r in results)}.\n"
                    "Подробности и неудачные попытки — в результате импорта.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="Результат импорта",
                                    callback_data=f"import:result:{active.batch_id}",
                                )
                            ],
                            [InlineKeyboardButton(text="Аккаунты", callback_data="accounts:all:0")],
                        ]
                    ),
                )
            if progress:
                with suppress(Exception):
                    await progress.edit_reply_markup(reply_markup=None)

    async def _qr(self, active, item, index, total, reauth_id):
        return await self._login(active, item, index, total, reauth_id, "qr")

    async def _phone(self, active, item, index, total, reauth_id):
        return await self._login(active, item, index, total, reauth_id, "code")

    async def _login(self, active, item, index, total, reauth_id, method):
        if reauth_id:
            await self.accounts.set_status(reauth_id, AccountStatus.DISABLED, active.actor)
        proxy = await self.runtime.proxy_for(reauth_id)
        client = build_user_client(self.settings, "", proxy, receive_updates=True)
        qr_message = None
        stored = False
        disconnected = False
        try:
            async with (
                asyncio.timeout(self.settings.login_ttl_seconds),
                self.runtime.bootstrap(active.current),
            ):
                await cancellable(
                    client.connect(), active.current, self.settings.rpc_timeout_seconds
                )
                if method == "code":
                    me = await self._code_user(active, client, item, index, total)
                while method == "qr":
                    qr = await cancellable(
                        client.qr_login(), active.current, self.settings.rpc_timeout_seconds
                    )
                    buffer = io.BytesIO()
                    qrcode.make(qr.url).save(buffer, format="PNG")
                    target = f" для {escape(item.value)}" if item.kind == "phone" else ""
                    qr_message = await self.bot.send_photo(
                        active.actor,
                        BufferedInputFile(buffer.getvalue(), "login.png"),
                        caption=f"Аккаунт {index}/{total}{target}.\nВ нужном аккаунте Telegram: Настройки → Устройства → Подключить устройство. Отсканируйте QR.\n"
                        "QR даёт доступ к аккаунту: не пересылайте его. Он обновляется автоматически.",
                        reply_markup=self.keyboard(active),
                        protect_content=True,
                    )
                    try:
                        me = await cancellable(qr.wait(timeout=55), active.current, 60)
                        break
                    except SessionPasswordNeededError:
                        me = await self._password(active, client)
                        break
                    except TimeoutError:
                        pass
                    finally:
                        with suppress(Exception):
                            await qr_message.delete()
                        qr_message = None
                if not me or me.bot or not me.phone:
                    raise UserInputError("Нужен пользовательский аккаунт")
                if (
                    item.kind == "phone"
                    and normalize_phone("+" + me.phone.lstrip("+")) != item.value
                ):
                    raise UserInputError(
                        "Подтверждён другой номер. Повторите вход для нужного аккаунта."
                    )
                async with self.db.session() as session:
                    existing = await session.scalar(
                        select(TelegramAccount).where(TelegramAccount.telegram_user_id == me.id)
                    )
                    if reauth_id and (
                        not existing or existing.id != reauth_id or existing.archived_at
                    ):
                        raise UserInputError("Авторизован другой аккаунт или аккаунт уже удалён")
                if existing and not existing.archived_at and existing.id != reauth_id:
                    await cancellable(client.log_out(), active.current, 10)
                    disconnected = True
                    return existing, False
                saved_session = client.session.save()
                await asyncio.wait_for(client.disconnect(), 10)
                disconnected = True
                result = await self.accounts.store_verified(
                    saved_session, me, active.actor, proxy, reauth_id=reauth_id
                )
                stored = result[1]
                return result
        finally:
            if qr_message:
                with suppress(Exception):
                    await qr_message.delete()
            if not stored and not disconnected:
                # This client owns a newly generated key; never log out an imported session.
                with suppress(Exception, asyncio.CancelledError):
                    if await asyncio.wait_for(client.is_user_authorized(), 5):
                        await asyncio.wait_for(client.log_out(), 10)
            if not disconnected:
                with suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(client.disconnect(), 10)

    async def _code_user(self, active, client, item, index, total):
        phone = normalize_phone(item.value)
        sent = await cancellable(
            client.send_code_request(phone), active.current, self.settings.rpc_timeout_seconds
        )
        resend_at = time.monotonic() + max(0, getattr(sent, "timeout", None) or 60)
        requests, attempts = 1, 0
        notice = ""
        while attempts < 3:
            kind = type(getattr(sent, "type", None)).__name__
            destinations = {
                "SentCodeTypeApp": "Код отправлен в Telegram на уже подключённое устройство.",
                "SentCodeTypeSms": "Код отправлен по SMS.",
                "SentCodeTypeCall": "Telegram сообщит код голосовым звонком.",
                "SentCodeTypeMissedCall": "Введите последние цифры номера пропущенного звонка.",
                "SentCodeTypeFragmentSms": "Откройте Fragment для получения кода.",
            }
            length = getattr(getattr(sent, "type", None), "length", None)
            if kind not in destinations or not isinstance(length, int) or not 1 <= length <= 12:
                raise UserInputError(
                    "Telegram запросил неподдерживаемый способ подтверждения. "
                    "Используйте вход по QR или готовую сессию."
                )
            code = await self._ask_code(
                active,
                length,
                resend_at,
                bool(getattr(sent, "next_type", None)) and requests < 3,
                f"Аккаунт {index}/{total}: {escape(phone)}.\n{notice}{destinations[kind]}\n"
                f"Наберите {length} цифр кнопками ниже и нажмите «Войти». "
                "Цифры не отображаются в сообщении. Не пересылайте код в чат. "
                "Обычный чат бота не имеет сквозного шифрования.",
            )
            if code is None:
                sent = await cancellable(
                    client.send_code_request(phone),
                    active.current,
                    self.settings.rpc_timeout_seconds,
                )
                requests += 1
                resend_at = time.monotonic() + max(0, getattr(sent, "timeout", None) or 60)
                notice = "Запрошен новый код. Предыдущий больше не используйте.\n"
                continue
            attempts += 1
            try:
                return await cancellable(
                    client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash),
                    active.current,
                    self.settings.rpc_timeout_seconds,
                )
            except SessionPasswordNeededError:
                code = None
                return await self._password(active, client)
            except (PhoneCodeInvalidError, PhoneCodeEmptyError):
                notice = f"Код не подошёл. Осталось попыток: {3 - attempts}.\n"
            except PhoneCodeExpiredError as exc:
                raise UserInputError(
                    "Код истёк или аннулирован. Начните вход по коду заново."
                ) from exc
            finally:
                code = None
        raise UserInputError("Лимит попыток кода исчерпан. Начните вход заново.")

    async def _ask_code(self, active, length, resend_at, can_resend, text):
        prompt = None
        code = CodeInput(
            secrets.token_hex(6),
            asyncio.get_running_loop().create_future(),
            length,
            resend_at,
            can_resend,
        )
        active.code = code
        try:
            prompt = await self.bot.send_message(
                active.actor,
                text,
                reply_markup=self.code_keyboard(active, code),
                protect_content=True,
            )
            code.message_id = prompt.message_id
            return await cancellable(code.result, active.current, self.settings.login_ttl_seconds)
        finally:
            code.digits = ""
            if not code.result.done():
                code.result.cancel()
            if active.code is code:
                active.code = None
            if prompt:
                with suppress(Exception):
                    await prompt.delete()

    async def _password(self, active, client):
        prompt = None
        try:
            for attempt in range(3):
                active.password = asyncio.get_running_loop().create_future()
                prompt = await self.bot.send_message(
                    active.actor,
                    (
                        "У аккаунта включена двухэтапная аутентификация. "
                        if attempt == 0
                        else "Пароль не подошёл. "
                    )
                    + "Отправьте пароль следующим сообщением. Бот удалит его перед проверкой и не запишет на диск. "
                    "Обычный чат Telegram не обеспечивает сквозное шифрование. Если это неприемлемо, нажмите «Пропустить» и импортируйте готовую сессию.",
                    reply_markup=self.keyboard(active),
                    protect_content=True,
                )
                password = await cancellable(
                    active.password, active.current, self.settings.login_ttl_seconds
                )
                active.password = None
                try:
                    return await cancellable(
                        client.sign_in(password=password),
                        active.current,
                        self.settings.rpc_timeout_seconds,
                    )
                except PasswordHashInvalidError:
                    pass
                finally:
                    password = None
                    with suppress(Exception):
                        await prompt.delete()
                    prompt = None
            raise UserInputError("Лимит попыток пароля исчерпан")
        finally:
            if active.password and not active.password.done():
                active.password.cancel()
            active.password = None
            if prompt:
                with suppress(Exception):
                    await prompt.delete()

    async def shutdown(self):
        self.pending = None
        if self.active and self.active.task:
            self.active.stopping = True
            self.active.task.cancel()
            await asyncio.gather(self.active.task, return_exceptions=True)
