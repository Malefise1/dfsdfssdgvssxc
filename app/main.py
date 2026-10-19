from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from sqlalchemy import text

from app.bot.handlers import BotServices, create_router
from app.config import get_settings
from app.db import Database
from app.logging_setup import configure_logging
from app.models import utcnow
from app.security import SecretBox
from app.services.accounts import AccountService
from app.services.jobs import JobRunner, PermanentWorker
from app.services.maintenance import (
    BackupWorker,
    HealthWorker,
    RetentionWorker,
    ScheduleService,
    ScheduleWorker,
    periodic,
)
from app.services.onboarding import OnboardingService
from app.services.proxies import ProxyService
from app.services.reports import ReportService
from app.services.runtime import AccountRuntime
from app.services.subscriptions import SubscriptionService

logger = logging.getLogger(__name__)


async def main():
    settings = get_settings()
    settings.ensure_directories()
    configure_logging(settings)
    # Local lock complements the database lock and prevents split-brain on a DB reconnect.
    import fcntl

    local_lock = (settings.data_dir / "instance.lock").open("a")
    try:
        fcntl.flock(local_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        local_lock.close()
        raise RuntimeError("Уже работает процесс с этим DATA_DIR") from None
    db = Database(settings)
    bot = None
    workers = []
    stop = asyncio.Event()
    fatal = asyncio.Event()
    runner = runtime = onboarding = services = None
    try:
        await db.acquire_instance()
        await db.check_schema()
        await db.sync_owner(settings.main_admin_id)
        secrets = SecretBox(settings.session_encryption_key)
        runtime = AccountRuntime(settings, db, secrets)
        reports = ReportService(settings, db)
        accounts = AccountService(settings, db, secrets, runtime)
        await accounts.backfill_fingerprints()
        proxies = ProxyService(db, secrets)
        runner = JobRunner(settings, db, secrets, reports, runtime)
        subscriptions = SubscriptionService(settings, db, secrets, runtime)
        bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        onboarding = OnboardingService(settings, db, accounts, runtime, bot)
        await onboarding.recover()
        retention = RetentionWorker(settings, db, bot)
        backups = BackupWorker(settings)
        services = BotServices(
            settings,
            db,
            runner,
            runtime,
            accounts,
            proxies,
            reports,
            retention,
            subscriptions,
            ScheduleService(db),
            onboarding,
            backups,
        )
        dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
        dispatcher.include_router(create_router(services))
        permanent = PermanentWorker(settings, db, runner)
        schedules = ScheduleWorker(db, runner)
        health_worker = HealthWorker(settings, db, accounts, proxies)
        for name, interval, function in [
            ("permanent", 15, permanent.tick),
            ("schedules", 15, schedules.tick),
            ("backup", 1800, backups.tick),
            ("retention", 3600, retention.send_reminders),
            ("account_health", 60, health_worker.tick),
        ]:
            workers.append(
                asyncio.create_task(
                    periodic(name, interval, function, stop, services.health), name=name
                )
            )

        async def guard():
            notified = {}
            while not stop.is_set():
                try:
                    async with asyncio.timeout(5):
                        held = await db._instance_connection.scalar(
                            text(
                                "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=781263011 AND pid=pg_backend_pid() AND granted"
                            )
                        )
                    if not held:
                        raise RuntimeError("Потеряна блокировка экземпляра")
                except Exception:
                    logger.critical("Instance lock connection lost; stopping service")
                    fatal.set()
                    runtime.closing = True
                    with suppress(RuntimeError):
                        await dispatcher.stop_polling()
                    return
                onboarding.expire()
                day = utcnow().date().isoformat()
                for name, state in services.health.copy().items():
                    if not state["ok"] and notified.get(name) != day:
                        with suppress(Exception):
                            await bot.send_message(
                                settings.main_admin_id,
                                f"Сбой фоновой задачи «{name}». Откройте «Состояние сервиса».",
                            )
                            notified[name] = day
                for account_id, message in runtime.activity.copy().items():
                    if message.startswith("Ожидание"):
                        key = f"flood:{account_id}"
                        if notified.get(key) != day:
                            from app.models import TelegramAccount
                            from app.services.control import aware

                            async with db.session() as session:
                                account = await session.get(TelegramAccount, account_id)
                            if (
                                account
                                and account.flood_until
                                and (aware(account.flood_until) - utcnow()).total_seconds()
                                >= settings.floodwait_notify_seconds
                            ):
                                with suppress(Exception):
                                    await bot.send_message(
                                        settings.main_admin_id,
                                        f"Аккаунт №{account.display_number}: длительное ожидание Telegram. Прокси автоматически не меняется.",
                                    )
                                    notified[key] = day
                try:
                    await asyncio.wait_for(stop.wait(), 3)
                except TimeoutError:
                    pass

        workers.append(asyncio.create_task(guard(), name="instance-guard"))
        resumed = await runner.resume_incomplete()
        if fatal.is_set():
            raise RuntimeError("Потеряно соединение блокировки экземпляра")
        logger.info("Service 2.0.0 started; resumed jobs: %s", resumed)
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=False,
            tasks_concurrency_limit=30,
        )
        if fatal.is_set():
            raise RuntimeError("Потеряно соединение блокировки экземпляра")
    finally:
        stop.set()
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        if onboarding:
            await onboarding.shutdown()
        if runtime:
            runtime.closing = True
        if runner:
            with suppress(TimeoutError):
                await runner.shutdown()
        if services:
            for task in list(services.background):
                task.cancel()
            await asyncio.gather(*services.background, return_exceptions=True)
        if runtime:
            await runtime.stop_all()
        if bot:
            await bot.session.close()
        with suppress(Exception):
            await db.close()
        local_lock.close()


if __name__ == "__main__":
    asyncio.run(main())
