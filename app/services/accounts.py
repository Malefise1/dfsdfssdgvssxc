from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
from contextlib import suppress

from sqlalchemy import select
from telethon.sessions import StringSession

from app.enums import AccountStatus
from app.errors import AUTH_ERRORS, UserInputError, safe_error
from app.models import TelegramAccount, utcnow
from app.services.control import audit, aware
from app.telegram.client import build_user_client


def normalize_phone(raw):
    value = re.sub(r"[\s()\-]", "", raw.strip())
    if not re.fullmatch(r"\+[1-9][0-9]{6,14}", value):
        raise UserInputError("Номер должен быть в международном формате: +79991234567")
    return value


def smallest_free_account_number(used_numbers):
    used = {number for number in used_numbers if number > 0}
    number = 1
    while number in used:
        number += 1
    return number


def fingerprint(session_string):
    session = StringSession(session_string.strip())
    if not session.server_address or not ipaddress.ip_address(session.server_address).is_global:
        raise UserInputError("Сессия должна содержать публичный адрес дата-центра Telegram")
    if not session.auth_key or len(session.auth_key.key) != 256:
        raise UserInputError("Некорректная сессия Telethon")
    return hashlib.sha256(session.auth_key.key).hexdigest()


class AccountService:
    def __init__(self, settings, db, secrets, runtime):
        self.settings, self.db, self.secrets, self.runtime = settings, db, secrets, runtime
        self._import_lock = asyncio.Lock()
        self._store_lock = asyncio.Lock()

    async def backfill_fingerprints(self):
        async with self.db.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(TelegramAccount).where(
                            TelegramAccount.session_fingerprint.is_(None),
                            TelegramAccount.archived_at.is_(None),
                        )
                    )
                ).all()
            )
            seen = set(
                (
                    await session.scalars(
                        select(TelegramAccount.session_fingerprint).where(
                            TelegramAccount.session_fingerprint.is_not(None)
                        )
                    )
                ).all()
            )
            for row in rows:
                try:
                    value = fingerprint(self.secrets.decrypt(row.encrypted_session))
                    if value in seen:
                        row.status = AccountStatus.QUARANTINED
                        row.last_error = "Повтор ключа сессии; выполните новый вход"
                        row.health_ok = False
                    else:
                        row.session_fingerprint = value
                        seen.add(value)
                except Exception:
                    row.health_ok = False
                    row.last_error = (
                        "Сессия не читается: проверьте ключ шифрования или авторизуйте заново"
                    )

    async def import_session(self, session_string, actor_id, label=""):
        key = fingerprint(session_string)
        async with self._import_lock:
            async with self.db.session() as session:
                existing = await session.scalar(
                    select(TelegramAccount).where(TelegramAccount.session_fingerprint == key)
                )
                if existing and not existing.archived_at:
                    return existing, False
            proxy = await self.runtime.proxy_for()
            client = build_user_client(self.settings, session_string, proxy, 0)
            try:
                async with (
                    self.runtime.bootstrap(),
                    asyncio.timeout(self.settings.rpc_timeout_seconds),
                ):
                    await client.connect()
                    if not await client.is_user_authorized():
                        raise UserInputError("Сессия отозвана; нужен новый вход")
                    me = await client.get_me()
                    if not me or me.bot or not me.phone:
                        raise UserInputError("Нужна пользовательская сессия с номером телефона")
                    saved = client.session.save()
                    await asyncio.wait_for(client.disconnect(), 10)
                    return await self.store_verified(saved, me, actor_id, proxy, label)
            finally:
                with suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(client.disconnect(), 10)

    async def store_verified(
        self, session_string, me, actor_id, proxy=None, label="", reauth_id=None
    ):
        async with self._store_lock:
            return await self._store_verified(session_string, me, actor_id, proxy, label, reauth_id)

    async def _store_verified(self, session_string, me, actor_id, proxy, label, reauth_id):
        phone = normalize_phone("+" + me.phone.lstrip("+"))
        async with self.db.session() as session:
            existing = await session.scalar(
                select(TelegramAccount).where(TelegramAccount.telegram_user_id == me.id)
            )
            if existing and not existing.archived_at and existing.id != reauth_id:
                return existing, False
            if reauth_id and (not existing or existing.id != reauth_id):
                raise UserInputError(
                    "Авторизован другой аккаунт. Выберите нужный аккаунт в Telegram."
                )
            if existing:
                row = existing
                row.archived_at = None
            else:
                # Numbers of archived accounts remain reserved to keep historical identity stable.
                numbers = list(
                    (await session.scalars(select(TelegramAccount.display_number))).all()
                )
                row = TelegramAccount(
                    display_number=smallest_free_account_number(numbers), telegram_user_id=me.id
                )
                session.add(row)
            row.phone = phone
            row.encrypted_session = self.secrets.encrypt(session_string)
            row.session_fingerprint = fingerprint(session_string)
            row.label = (label or " ".join(filter(None, [me.first_name, me.last_name])))[:255]
            row.status, row.health_ok = AccountStatus.ACTIVE, True
            row.last_error, row.error_streak = None, 0
            row.health_checked_at = utcnow()
            row.last_proxy_id = proxy.id if proxy else None
            await session.flush()
        await audit(
            self.db,
            actor_id,
            "account.reauthorized" if reauth_id else "account.imported",
            row.id,
            number=row.display_number,
        )
        return row, True

    async def set_status(self, account_id, status, actor_id=0):
        async with self.db.session() as session:
            row = await session.get(TelegramAccount, account_id)
            if not row or row.archived_at:
                return False
            row.status = status
            if status == AccountStatus.ACTIVE:
                row.error_streak = 0
        if status != AccountStatus.ACTIVE:
            await self.runtime.cancel_account(account_id)
        await audit(self.db, actor_id, "account.status", account_id, status=status.value)
        return True

    async def delete(self, account_id, actor_id=0):
        await self.set_status(account_id, AccountStatus.DISABLED, actor_id)
        async with self.db.session() as session:
            row = await session.get(TelegramAccount, account_id)
            if not row:
                return False
            row.archived_at = utcnow()
            row.encrypted_session = ""
            row.phone = f"archived:{row.id}"
            row.session_fingerprint = None
            row.health_ok = False
        await audit(self.db, actor_id, "account.archived", account_id)
        return True

    async def check(self, account_id, actor_id=0):
        if account_id in self.runtime.activity:
            return False, "Аккаунт занят; проверка доступна после текущей операции"
        async with self.db.session() as session:
            row = await session.get(TelegramAccount, account_id)
            if row and row.flood_until and aware(row.flood_until) > utcnow():
                return False, "Аккаунт ожидает окончания ограничения Telegram"
        ok, message = False, ""
        try:
            async with self.runtime.connection(
                account_id, "Проверка аккаунта", allow_disabled=True
            ) as (client, _, _):
                me = await client.get_me()
                if not me or me.bot:
                    raise UserInputError("Неверная пользовательская сессия")
                ok = True
                message = "Авторизация и соединение работают"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = safe_error(exc)
            if isinstance(exc, AUTH_ERRORS):
                async with self.db.session() as session:
                    row = await session.get(TelegramAccount, account_id)
                    if row:
                        row.status = AccountStatus.QUARANTINED
        async with self.db.session() as session:
            row = await session.get(TelegramAccount, account_id)
            if row:
                row.health_checked_at, row.health_ok = utcnow(), ok
                row.last_error = None if ok else message
                if not ok:
                    row.last_error_at = utcnow()
        if actor_id:
            await audit(self.db, actor_id, "account.checked", account_id, ok=ok)
        return ok, message
