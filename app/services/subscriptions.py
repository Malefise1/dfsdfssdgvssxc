from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from telethon.errors import UserNotParticipantError
from telethon.tl.functions.channels import (
    GetParticipantRequest,
    JoinChannelRequest,
    LeaveChannelRequest,
)

from app.errors import PausedError, safe_error
from app.models import AccountChannelSubscription, Channel, TelegramAccount, utcnow
from app.repositories import list_active_accounts
from app.services.control import audit, paused

SubscriptionAction = Literal["subscribe", "unsubscribe"]


class SubscriptionCountError(ValueError):
    def __init__(self, requested, available):
        self.requested, self.available = requested, available
        super().__init__(f"Запрошено {requested}, доступно {available}")


@dataclass(frozen=True)
class SubscriptionAvailability:
    active_accounts: int
    subscribed_accounts: int
    available_to_subscribe: int
    available_to_unsubscribe: int


@dataclass(frozen=True)
class SubscriptionResult:
    action: str
    requested: int
    succeeded: tuple
    failed: tuple


def choose_random_items(items, count):
    if not 1 <= count <= len(items):
        raise SubscriptionCountError(count, len(items))
    return random.SystemRandom().sample(list(items), count)


class SubscriptionService:
    def __init__(self, settings, db, secrets, runtime):
        self.settings, self.db, self.secrets, self.runtime = settings, db, secrets, runtime
        self._operation_lock = asyncio.Lock()

    async def availability(self, channel_id):
        async with self.db.session() as session:
            accounts = await list_active_accounts(session)
            ids = set(
                (
                    await session.scalars(
                        select(AccountChannelSubscription.account_id)
                        .join(TelegramAccount)
                        .where(
                            AccountChannelSubscription.channel_id == channel_id,
                            AccountChannelSubscription.active.is_(True),
                            TelegramAccount.archived_at.is_(None),
                        )
                    )
                ).all()
            )
        active = {a.id for a in accounts}
        return SubscriptionAvailability(len(active), len(ids), len(active - ids), len(active & ids))

    async def run(self, channel_id, action, count, actor_id=0):
        if self._operation_lock.locked():
            raise ValueError("Уже выполняется операция подписки. Дождитесь результата.")
        async with self._operation_lock:
            if await paused(self.db):
                raise PausedError("Включена общая пауза")
            if action not in ("subscribe", "unsubscribe", "sync"):
                raise ValueError("Неизвестная операция")
            async with self.db.session() as session:
                channel = await session.get(Channel, channel_id)
                if not channel or channel.archived_at:
                    raise ValueError("Канал недоступен")
                accounts = await list_active_accounts(session)
                ids = set(
                    (
                        await session.scalars(
                            select(AccountChannelSubscription.account_id).where(
                                AccountChannelSubscription.channel_id == channel_id,
                                AccountChannelSubscription.active.is_(True),
                            )
                        )
                    ).all()
                )
            candidates = [
                a
                for a in accounts
                if action == "sync" or (a.id not in ids if action == "subscribe" else a.id in ids)
            ]
            chosen = candidates if action == "sync" else choose_random_items(candidates, count)
            await audit(
                self.db,
                actor_id,
                "subscription.started",
                channel_id,
                action_type=action,
                account_ids=[a.id for a in chosen],
            )
            results = await asyncio.gather(
                *(self._run_account(a, channel, action) for a in chosen), return_exceptions=True
            )
            good, failed = [], []
            for account, result in zip(chosen, results, strict=True):
                if result is None:
                    good.append(account.display_number)
                else:
                    failed.append(
                        (
                            account.display_number,
                            safe_error(result) if isinstance(result, BaseException) else result,
                        )
                    )
            await audit(
                self.db,
                actor_id,
                "subscription.finished",
                channel_id,
                action_type=action,
                succeeded=good,
                failed=failed,
            )
            return SubscriptionResult(action, len(chosen), tuple(good), tuple(failed))

    async def _run_account(self, account, channel, action):
        try:
            async with self.runtime.connection(account.id, "Подписки") as (client, _, _):
                entity = await client.get_entity(channel.username)
                if not getattr(entity, "broadcast", False):
                    raise ValueError("Это не публичный канал")
                if channel.telegram_id and entity.id != channel.telegram_id:
                    raise ValueError("Ссылка принадлежит другому каналу")
                if await paused(self.db):
                    raise PausedError("Операция остановлена")
                if action == "sync":
                    try:
                        await client(GetParticipantRequest(entity, "me"))
                        active = True
                    except UserNotParticipantError:
                        active = False
                else:
                    await client(
                        JoinChannelRequest(entity)
                        if action == "subscribe"
                        else LeaveChannelRequest(entity)
                    )
                    active = action == "subscribe"
                await self._store(account.id, channel.id, active, None)
                return None
        except asyncio.CancelledError:
            return "Операция остановлена"
        except Exception as exc:
            message = safe_error(exc)
            await self._store(account.id, channel.id, None, message)
            return message

    async def _store(self, account_id, channel_id, active, error):
        async with self.db.session() as session:
            row = await session.scalar(
                select(AccountChannelSubscription).where(
                    AccountChannelSubscription.account_id == account_id,
                    AccountChannelSubscription.channel_id == channel_id,
                )
            )
            if row is None:
                row = AccountChannelSubscription(
                    account_id=account_id, channel_id=channel_id, active=False
                )
                session.add(row)
            if active is not None:
                row.active = active
                if active:
                    row.subscribed_at, row.unsubscribed_at = utcnow(), None
                else:
                    row.unsubscribed_at = utcnow()
            row.last_error = error
