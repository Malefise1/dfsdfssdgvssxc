from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager, suppress
from datetime import timedelta

from sqlalchemy import delete, func, select
from telethon.errors import FloodWaitError

from app.enums import AccountStatus
from app.errors import AUTH_ERRORS, AccountUnavailable, ProxyUnavailable, SessionInvalid, safe_error
from app.models import AuditEvent, Proxy, TelegramAccount, utcnow
from app.services.control import aware, setting
from app.telegram.client import ProxyConfig, build_user_client


async def cancellable(awaitable, cancel=None, timeout=None):
    task = asyncio.ensure_future(awaitable)
    waiter = asyncio.create_task(cancel.wait()) if cancel else None
    try:
        waiters = {task, waiter} if waiter else {task}
        done, _ = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if waiter and waiter in done:
            raise asyncio.CancelledError
        if task not in done:
            raise TimeoutError
        return await task
    finally:
        for item in (task, waiter):
            if item and not item.done():
                item.cancel()
        await asyncio.gather(*(x for x in (task, waiter) if x), return_exceptions=True)


class ManagedClient:
    def __init__(self, runtime, account, client, cancel):
        self.runtime, self.account, self.raw, self.cancel = runtime, account, client, cancel

    async def request(self, factory):
        while True:
            try:
                return await cancellable(
                    factory(), self.cancel, self.runtime.settings.rpc_timeout_seconds
                )
            except FloodWaitError as exc:
                until = utcnow() + timedelta(seconds=exc.seconds)
                async with self.runtime.db.session() as session:
                    row = await session.get(TelegramAccount, self.account.id)
                    if row:
                        row.flood_until = until
                self.runtime.activity[self.account.id] = (
                    f"Ожидание Telegram до {until.isoformat(timespec='minutes')}"
                )
                await cancellable(asyncio.sleep(exc.seconds), self.cancel)

    async def __call__(self, request):
        return await self.request(lambda: self.raw(request))

    async def get_entity(self, value):
        return await self.request(lambda: self.raw.get_entity(value))

    async def get_input_entity(self, value):
        return await self.request(lambda: self.raw.get_input_entity(value))

    async def get_me(self):
        return await self.request(self.raw.get_me)

    def iter_messages(self, *args, **kwargs):
        iterator = self.raw.iter_messages(*args, **kwargs)

        async def iterate():
            while True:
                try:
                    item = await self.request(iterator.__anext__)
                except StopAsyncIteration:
                    return
                yield item

        return iterate()


class AccountRuntime:
    """All account network activity goes through this shared owner."""

    def __init__(self, settings, db, secrets):
        self.settings, self.db, self.secrets = settings, db, secrets
        self._locks = defaultdict(asyncio.Lock)
        self._slots = asyncio.Semaphore(settings.max_concurrent_accounts)
        self._tasks = defaultdict(set)
        self.activity = {}
        self.closing = False
        self._proxy_lock = asyncio.Lock()
        self._proxy_delete_lock = asyncio.Lock()
        self._deleting_proxies = set()

    @asynccontextmanager
    async def bootstrap(self, cancel=None):
        """New-session authentication shares the global connection budget."""
        if self.closing:
            raise AccountUnavailable("Сервис завершает работу")
        task = asyncio.current_task()
        self._tasks[0].add(task)
        acquired = False

        async def acquire():
            nonlocal acquired
            await self._slots.acquire()
            acquired = True

        try:
            await cancellable(acquire(), cancel)
            self.activity[0] = "Добавление аккаунта"
            yield
        finally:
            if acquired:
                self._slots.release()
                self.activity.pop(0, None)
            self._tasks[0].discard(task)
            if not self._tasks[0]:
                self._tasks.pop(0, None)

    async def proxy_for(self, account_id=None):
        # Selection and persistence are atomic across concurrent account operations.
        async with self._proxy_lock:
            return await self._proxy_for(account_id)

    async def set_proxy(self, account_id, proxy_id):
        """Change the route only, leaving authorization and account status untouched."""
        await self.cancel_account(account_id)
        async with self._locks[account_id], self._proxy_lock, self.db.session() as session:
            account = await session.get(TelegramAccount, account_id)
            if not account or account.archived_at:
                raise ValueError("Аккаунт не найден")
            if proxy_id:
                proxy = await session.get(Proxy, proxy_id)
                if (
                    not proxy
                    or proxy.id in self._deleting_proxies
                    or not proxy.active
                    or not proxy.last_check_ok
                    or not proxy.last_checked_at
                    or aware(proxy.last_checked_at)
                    < utcnow() - timedelta(hours=self.settings.proxy_check_max_age_hours)
                ):
                    raise ValueError("Сначала проверьте выбранный прокси")
            account.last_proxy_id = proxy_id or None

    async def delete_proxies(self, proxy_ids, actor_id):
        """Stop users of these routes, then delete proxies without touching session keys."""
        ids = sorted({int(value) for value in proxy_ids if int(value) > 0})
        if not ids:
            return {"deleted": 0, "accounts": 0, "paused": 0}
        async with self._proxy_delete_lock:
            if self._tasks.get(0):
                raise ValueError("Сначала завершите добавление аккаунтов")
            async with self._proxy_lock:
                async with self.db.session() as session:
                    existing = list(
                        (await session.scalars(select(Proxy.id).where(Proxy.id.in_(ids)))).all()
                    )
                    accounts = list(
                        (
                            await session.scalars(
                                select(TelegramAccount.id).where(
                                    TelegramAccount.last_proxy_id.in_(existing)
                                )
                            )
                        ).all()
                    )
                self._deleting_proxies.update(existing)
            try:
                # Do not hold the routing lock while waiting for cancelled tasks to unwind.
                await asyncio.gather(*(self.cancel_account(account_id) for account_id in accounts))
                async with self._proxy_lock, self.db.session() as session:
                    rows = list(
                        (
                            await session.scalars(
                                select(TelegramAccount)
                                .where(
                                    TelegramAccount.last_proxy_id.in_(existing),
                                    TelegramAccount.archived_at.is_(None),
                                )
                                .with_for_update()
                            )
                        ).all()
                    )
                    paused_count = 0
                    for account in rows:
                        if account.status == AccountStatus.ACTIVE:
                            account.status = AccountStatus.DISABLED
                            paused_count += 1
                        # Keep the old route ID as a guard: even an explicit health check
                        # must not fall back to the server IP or another proxy silently.
                    await session.execute(delete(Proxy).where(Proxy.id.in_(existing)))
                    result = {
                        "deleted": len(existing),
                        "accounts": len(rows),
                        "paused": paused_count,
                    }
                    session.add(
                        AuditEvent(
                            actor_id=actor_id,
                            action="proxies.deleted",
                            target="",
                            detail={"proxy_ids": existing, **result},
                        )
                    )
                return result
            finally:
                self._deleting_proxies.difference_update(existing)

    async def _proxy_for(self, account_id=None):
        policy = (await setting(self.db, "proxy_policy", {"value": self.settings.proxy_policy}))[
            "value"
        ]
        async with self.db.session() as session:
            account = await session.get(TelegramAccount, account_id) if account_id else None
            proxies = list(
                (
                    await session.scalars(
                        select(Proxy)
                        .where(
                            Proxy.active.is_(True),
                            Proxy.id.not_in(self._deleting_proxies),
                            Proxy.last_check_ok.is_(True),
                            Proxy.last_checked_at
                            >= utcnow() - timedelta(hours=self.settings.proxy_check_max_age_hours),
                        )
                        .order_by(Proxy.id)
                    )
                ).all()
            )
            if account and account.last_proxy_id:
                if not await session.get(Proxy, account.last_proxy_id):
                    raise ProxyUnavailable("Прокси удалён. Назначьте новый в карточке аккаунта.")
                row = next((p for p in proxies if p.id == account.last_proxy_id), None)
                if not row:
                    raise ProxyUnavailable(
                        "Закреплённый прокси не прошёл свежую проверку. Проверьте или смените его."
                    )
            elif proxies:
                counts = dict(
                    (
                        await session.execute(
                            select(TelegramAccount.last_proxy_id, func.count())
                            .where(TelegramAccount.archived_at.is_(None))
                            .group_by(TelegramAccount.last_proxy_id)
                        )
                    ).all()
                )
                row = min(proxies, key=lambda p: (counts.get(p.id, 0), p.id))
                if account:
                    account.last_proxy_id = row.id
            else:
                if policy == "required":
                    raise ProxyUnavailable(
                        "Нет проверенных прокси. Загрузите и проверьте прокси либо явно разрешите прямое подключение."
                    )
                return None
            return ProxyConfig(
                row.id,
                row.protocol,
                row.host,
                row.port,
                row.login,
                self.secrets.decrypt(row.encrypted_password),
            )

    @asynccontextmanager
    async def connection(self, account_id, purpose, cancel=None, allow_disabled=False):
        if self.closing:
            raise AccountUnavailable("Сервис завершает работу")
        task = asyncio.current_task()
        self._tasks[account_id].add(task)
        lock = self._locks[account_id]
        locked = slotted = False
        client = None
        try:

            async def acquire_lock():
                nonlocal locked
                await lock.acquire()
                locked = True

            async def acquire_slot():
                nonlocal slotted
                await self._slots.acquire()
                slotted = True

            await cancellable(acquire_lock(), cancel)
            await cancellable(acquire_slot(), cancel)
            async with self.db.session() as session:
                account = await session.get(TelegramAccount, account_id)
                if not account or account.archived_at:
                    raise AccountUnavailable("Аккаунт удалён из работы")
                if not allow_disabled and account.status != AccountStatus.ACTIVE:
                    raise AccountUnavailable("Аккаунт остановлен или требует внимания")
            if account.flood_until and aware(account.flood_until) > utcnow():
                self.activity[account_id] = "Ожидание ограничения Telegram"
                await cancellable(
                    asyncio.sleep((aware(account.flood_until) - utcnow()).total_seconds()), cancel
                )
            self.activity[account_id] = purpose
            proxy = await self.proxy_for(account_id)
            try:
                session_string = self.secrets.decrypt(account.encrypted_session)
                client = build_user_client(
                    self.settings, session_string, proxy, account.display_number
                )
            except ValueError as exc:
                raise SessionInvalid("Сессия повреждена или не подходит ключ шифрования") from exc
            await cancellable(client.connect(), cancel, self.settings.rpc_timeout_seconds)
            if not await cancellable(
                client.is_user_authorized(), cancel, self.settings.rpc_timeout_seconds
            ):
                raise SessionInvalid("Сессия отозвана; нужна повторная авторизация")
            yield ManagedClient(self, account, client, cancel), account, proxy
        except AUTH_ERRORS as exc:
            async with self.db.session() as session:
                row = await session.get(TelegramAccount, account_id)
                if row:
                    row.status = AccountStatus.QUARANTINED
                    row.health_ok = False
                    row.last_error, row.last_error_at = safe_error(exc), utcnow()
            raise
        finally:
            if client:
                with suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(client.disconnect(), 10)
            if slotted:
                self._slots.release()
            if locked:
                lock.release()
                self.activity.pop(account_id, None)
            self._tasks[account_id].discard(task)
            if not self._tasks[account_id]:
                self._tasks.pop(account_id, None)

    async def cancel_account(self, account_id):
        tasks = [t for t in self._tasks.get(account_id, ()) if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    async def stop_all(self):
        tasks = {
            t for group in self._tasks.values() for t in group if t is not asyncio.current_task()
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
