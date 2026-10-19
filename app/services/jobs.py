from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import func, select, update

from app.enums import AccountStatus, JobMode, JobStatus, ResultStatus
from app.errors import AUTH_ERRORS, PausedError, error_kind, safe_error
from app.models import AccountJobResult, Channel, Job, Schedule, TelegramAccount, utcnow
from app.repositories import (
    aggregate_job,
    create_job,
    get_job_channels,
    get_progress,
    has_running_job_for_channel,
    job_results,
    list_active_accounts,
    upsert_channel,
)
from app.services.access import require_channels
from app.services.control import audit, aware, paused, put_setting
from app.services.runtime import cancellable
from app.telegram.viewer import ChannelPaused, PublicChannelUnavailable, view_public_channel

logger = logging.getLogger(__name__)
ACTIVE_JOBS = (JobStatus.PENDING, JobStatus.RUNNING)


class JobRunner:
    def __init__(self, settings, db, secrets, reports, runtime):
        self.settings, self.db, self.secrets, self.reports, self.runtime = (
            settings,
            db,
            secrets,
            reports,
            runtime,
        )
        self._tasks, self._cancel_events = {}, {}
        self._create_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(3)
        self.shutting_down = False

    async def create_from_links(self, links, **kwargs):
        async with self.db.session() as session:
            ids = [
                (await upsert_channel(session, link.username, link.url, kwargs["created_by"])).id
                for link in links
            ]
        return await self.create_from_channel_ids(ids, **kwargs)

    async def create_from_channel_ids(
        self,
        channel_ids,
        *,
        mode,
        created_by,
        duration_minutes=None,
        automatic=False,
        restart_full=True,
        schedule_id=None,
    ):
        ids = list(dict.fromkeys(channel_ids))
        await require_channels(self.settings, self.db, created_by, ids)
        if not 1 <= len(ids) <= self.settings.max_channels_per_job:
            raise ValueError(f"Выберите от 1 до {self.settings.max_channels_per_job} каналов")
        if mode == JobMode.TEST and (
            not isinstance(duration_minutes, int) or not 1 <= duration_minutes <= 10080
        ):
            raise ValueError("Длительность теста: от 1 до 10080 минут")
        async with self._create_lock:
            if self.shutting_down or await paused(self.db):
                raise PausedError(
                    "Включена общая пауза. Главный администратор может возобновить работу."
                )
            async with self.db.session() as session:
                schedule = None
                if schedule_id:
                    schedule = await session.scalar(
                        select(Schedule).where(Schedule.id == schedule_id).with_for_update()
                    )
                    if not schedule or not schedule.enabled or aware(schedule.run_at) > utcnow():
                        raise PausedError("Расписание изменилось")
                channels = list(
                    (
                        await session.scalars(
                            select(Channel).where(
                                Channel.id.in_(ids), Channel.archived_at.is_(None)
                            )
                        )
                    ).all()
                )
                if len(channels) != len(ids):
                    raise ValueError("Канал недоступен или находится в архиве")
                if automatic and any(
                    [await has_running_job_for_channel(session, c.id) for c in channels]
                ):
                    raise PausedError("Предыдущее задание канала ещё выполняется")
                count = await session.scalar(
                    select(func.count()).select_from(Job).where(Job.status.in_(ACTIVE_JOBS))
                )
                if count >= self.settings.max_queued_jobs:
                    raise ValueError(
                        "Очередь заполнена. Дождитесь завершения или остановите задания."
                    )
                accounts = await list_active_accounts(session)
                if not accounts:
                    raise ValueError("Нет активных аккаунтов")
                if len(accounts) * len(channels) > 50000:
                    raise ValueError(
                        "Слишком большое задание: разделите каналы на несколько запусков"
                    )
                job = await create_job(session, mode, ids, created_by, duration_minutes)
                job.expected_results = len(accounts) * len(channels)
                if schedule:
                    schedule.last_job_id, schedule.last_run_at, schedule.last_error = (
                        job.id,
                        utcnow(),
                        None,
                    )
                    if schedule.kind.value == "once":
                        schedule.enabled = False
                    else:
                        schedule.run_at = utcnow() + timedelta(minutes=schedule.interval_minutes)
                for channel in channels:
                    if mode == JobMode.PERMANENT_CYCLE and not automatic:
                        channel.permanent, channel.active = True, True
                        channel.last_cycle_at = utcnow()
                    for account in accounts:
                        progress = await get_progress(session, account.id, channel.id)
                        continue_cycle = mode == JobMode.PERMANENT_CYCLE and (
                            automatic or not restart_full
                        )
                        cursor = (
                            progress.last_viewed_message_id
                            if continue_cycle and progress.last_full_scan_at
                            else 0
                        )
                        session.add(
                            AccountJobResult(
                                job_id=job.id,
                                account_id=account.id,
                                channel_id=channel.id,
                                status=ResultStatus.PARTIAL,
                                state="queued",
                                account_number=account.display_number,
                                account_phone=account.phone,
                                channel_title=channel.title or "@" + channel.username,
                                channel_url=channel.url,
                                last_message_id=cursor,
                                full_scan_complete=bool(cursor and progress.last_full_scan_at),
                            )
                        )
            await audit(
                self.db,
                created_by,
                "job.created",
                job.id,
                mode=mode.value,
                channels=ids,
                accounts=len(accounts),
                automatic=automatic,
            )
            return job

    def launch(self, job_id):
        if self.shutting_down or (job_id in self._tasks and not self._tasks[job_id].done()):
            return
        self._cancel_events[job_id] = asyncio.Event()
        task = asyncio.create_task(self.run(job_id), name=f"job:{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda task: self._done(job_id, task))

    def _done(self, job_id, task):
        self._tasks.pop(job_id, None)
        self._cancel_events.pop(job_id, None)
        if not task.cancelled() and task.exception():
            logger.error("Job %s failed: %s", job_id, type(task.exception()).__name__)

    async def stop_job(self, job_id, actor_id=0):
        async with self._create_lock:
            async with self.db.session() as session:
                job = await session.get(Job, job_id, with_for_update=True)
                if not job or job.status not in ACTIVE_JOBS:
                    return False
                job.cancel_requested, job.stop_reason = True, "user"
            event = self._cancel_events.get(job_id)
            if event:
                event.set()
            else:
                await self._finalize(job_id, cancelled=True)
        await audit(self.db, actor_id, "job.cancel_requested", job_id)
        return True

    async def stop_all(self, actor_id=0):
        async with self._create_lock:
            await put_setting(self.db, "global_pause", {"paused": True})
            async with self.db.session() as session:
                ids = list(
                    (await session.scalars(select(Job.id).where(Job.status.in_(ACTIVE_JOBS)))).all()
                )
                await session.execute(
                    update(Job)
                    .where(Job.id.in_(ids), Job.status.in_(ACTIVE_JOBS))
                    .values(cancel_requested=True, stop_reason="global_pause")
                )
            for event in self._cancel_events.values():
                event.set()
        await self.runtime.stop_all()
        for job_id in ids:
            if job_id not in self._tasks:
                await self._finalize(job_id, cancelled=True)
        await audit(self.db, actor_id, "system.paused", count=len(ids))
        return len(ids)

    async def resume_permanent(self, actor_id=0):
        async with self._create_lock:
            await put_setting(self.db, "global_pause", {"paused": False})
        await audit(self.db, actor_id, "system.resumed")
        return await self.resume_incomplete()

    async def resume_incomplete(self):
        async with self.db.session() as session:
            jobs = list(
                (await session.scalars(select(Job).where(Job.status.in_(ACTIVE_JOBS)))).all()
            )
        resumed = 0
        for job in jobs:
            if job.cancel_requested:
                await self._finalize(job.id, cancelled=True)
            elif not await paused(self.db):
                self.launch(job.id)
                resumed += 1
        return resumed

    async def shutdown(self):
        self.shutting_down = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                self.settings.shutdown_timeout_seconds,
            )

    async def run(self, job_id):
        event = self._cancel_events.setdefault(job_id, asyncio.Event())
        children = []
        timer = None
        slotted = False
        try:

            async def acquire():
                nonlocal slotted
                await self._slots.acquire()
                slotted = True

            await cancellable(acquire(), event)
            if await paused(self.db):
                async with self.db.session() as session:
                    job = await session.get(Job, job_id)
                if job and job.cancel_requested:
                    await self._finalize(job_id, cancelled=True)
                return
            async with self.db.session() as session:
                job = await session.get(Job, job_id, with_for_update=True)
                if not job or job.status not in ACTIVE_JOBS:
                    return
                if job.cancel_requested:
                    event.set()
                job.status = JobStatus.RUNNING
                job.started_at = job.started_at or utcnow()
                if job.mode == JobMode.TEST:
                    job.ends_at = job.ends_at or (
                        aware(job.started_at) + timedelta(minutes=job.duration_minutes)
                    )
                results = await job_results(session, job_id)
                channels = await get_job_channels(session, job_id)
            if job.ends_at:

                async def deadline():
                    await asyncio.sleep(max(0, (aware(job.ends_at) - utcnow()).total_seconds()))
                    event.set()

                timer = asyncio.create_task(deadline())
            ids = sorted({r.account_id for r in results if r.state != "done"})
            children = [
                asyncio.create_task(
                    self._account(job, id_, channels, event), name=f"job:{job.id}:account:{id_}"
                )
                for id_ in ids
            ]
            outcomes = await asyncio.gather(*children, return_exceptions=True)
            for result in outcomes:
                if isinstance(result, BaseException):
                    logger.error("Account worker exception: %s", type(result).__name__)
            await self._finalize(job_id)
        except asyncio.CancelledError:
            for child in children:
                child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            if not self.shutting_down:
                await self._finalize(job_id)
            else:
                async with self.db.session() as session:
                    job = await session.get(Job, job_id)
                    if job and job.status in ACTIVE_JOBS:
                        job.status = JobStatus.PENDING
        except Exception as exc:
            logger.error("Job %s interrupted: %s", job_id, type(exc).__name__)
            await self._mark_remaining(
                job_id, None, ResultStatus.FAILED, safe_error(exc), error_kind(exc)
            )
            await self._finalize(job_id)
        finally:
            if timer:
                timer.cancel()
                await asyncio.gather(timer, return_exceptions=True)
            if slotted:
                self._slots.release()

    async def _account(self, job, account_id, channels, event):
        try:
            async with self.runtime.connection(account_id, f"Задание {job.id[:8]}", event) as (
                client,
                account,
                proxy,
            ):
                while not event.is_set():
                    needs_scan = False
                    for channel in channels:
                        if event.is_set():
                            break
                        async with self.db.session() as session:
                            result = await session.scalar(
                                select(AccountJobResult).where(
                                    AccountJobResult.job_id == job.id,
                                    AccountJobResult.account_id == account_id,
                                    AccountJobResult.channel_id == channel.id,
                                )
                            )
                            if not result or result.state == "done":
                                continue
                            result.state, result.proxy_id = "running", proxy.id if proxy else None
                            cursor, rid = result.last_message_id, result.id
                        self.runtime.activity[account_id] = f"{job.id[:8]} / @{channel.username}"

                        async def progress(cursor, count, rid=rid, channel_id=channel.id):
                            async with self.db.session() as session:
                                row = await session.get(AccountJobResult, rid)
                                row.last_message_id = cursor
                                row.viewed_posts += count
                                row.available_posts += count
                                stored = await get_progress(session, account_id, channel_id)
                                stored.last_viewed_message_id = max(
                                    stored.last_viewed_message_id, cursor
                                )
                                stored.total_registered += count

                        async def before_batch(channel_id=channel.id):
                            async with self.db.session() as session:
                                stored = await session.get(Channel, channel_id)
                                if (
                                    not stored
                                    or stored.archived_at
                                    or (job.mode == JobMode.PERMANENT_CYCLE and not stored.active)
                                ):
                                    raise ChannelPaused("Канал остановлен")

                        async def resolve(telegram_id, channel=channel):
                            async with self.db.session() as session:
                                stored = await session.scalar(
                                    select(Channel)
                                    .where(Channel.id == channel.id)
                                    .with_for_update()
                                )
                                if not stored or stored.archived_at:
                                    raise PublicChannelUnavailable("Канал в архиве")
                                if stored.telegram_id and stored.telegram_id != telegram_id:
                                    raise PublicChannelUnavailable(
                                        "Адрес принадлежит другому каналу"
                                    )
                                stored.telegram_id = telegram_id
                                channel.telegram_id = telegram_id

                        try:
                            await before_batch()
                            outcome = await view_public_channel(
                                client,
                                channel.username,
                                start_after_id=cursor,
                                cancel_event=event,
                                on_progress=progress,
                                expected_channel_id=channel.telegram_id,
                                on_resolve=resolve,
                                before_batch=before_batch,
                                max_messages=100 if job.mode == JobMode.TEST else None,
                            )
                            needs_scan = needs_scan or not outcome.scan_complete
                            async with self.db.session() as session:
                                row = await session.get(AccountJobResult, rid)
                                row.channel_title = outcome.channel_title
                                row.available_posts += max(
                                    0, outcome.available_posts - outcome.viewed_posts
                                )
                                row.full_scan_complete = (
                                    row.full_scan_complete or not outcome.cancelled
                                )
                                row.status = (
                                    ResultStatus.COMPLETED
                                    if not outcome.cancelled
                                    else ResultStatus.PARTIAL
                                )
                                row.state = "watching" if job.mode == JobMode.TEST else "done"
                                if row.state == "done":
                                    row.finished_at = utcnow()
                                stored_channel = await session.get(Channel, channel.id)
                                if stored_channel:
                                    if stored_channel.telegram_id is None:
                                        stored_channel.telegram_id = outcome.channel_id
                                    stored_channel.title = outcome.channel_title
                                    channel.telegram_id = stored_channel.telegram_id
                                stored_progress = await get_progress(
                                    session, account_id, channel.id
                                )
                                if not outcome.cancelled and outcome.scan_complete:
                                    stored_progress.last_full_scan_at = (
                                        stored_progress.last_full_scan_at or utcnow()
                                    )
                                stored_account = await session.get(TelegramAccount, account_id)
                                if stored_account:
                                    stored_account.error_streak = 0
                                    stored_account.last_error = None
                                    stored_account.health_ok, stored_account.health_checked_at = (
                                        True,
                                        utcnow(),
                                    )
                        except ChannelPaused:
                            async with self.db.session() as session:
                                row = await session.get(AccountJobResult, rid)
                                row.status, row.state = ResultStatus.CANCELLED, "done"
                                row.error_kind, row.error = "channel_paused", "Канал остановлен"
                                row.finished_at = utcnow()
                        except asyncio.CancelledError:
                            raise
                        except AUTH_ERRORS:
                            raise
                        except Exception as exc:
                            async with self.db.session() as session:
                                row = await session.get(AccountJobResult, rid)
                                row.status, row.state, row.error, row.error_kind = (
                                    ResultStatus.FAILED,
                                    "done",
                                    safe_error(exc),
                                    (
                                        "channel"
                                        if isinstance(exc, PublicChannelUnavailable)
                                        else error_kind(exc)
                                    ),
                                )
                                row.finished_at = utcnow()
                    if job.mode != JobMode.TEST:
                        break
                    async with self.db.session() as session:
                        left = await session.scalar(
                            select(func.count())
                            .select_from(AccountJobResult)
                            .where(
                                AccountJobResult.job_id == job.id,
                                AccountJobResult.account_id == account_id,
                                AccountJobResult.state != "done",
                            )
                        )
                    if not left:
                        break
                    await cancellable(
                        asyncio.sleep(0.2 if needs_scan else self.settings.check_interval_seconds),
                        event,
                    )
        except asyncio.CancelledError:
            if not self.shutting_down:
                timed = job.mode == JobMode.TEST and job.ends_at and utcnow() >= aware(job.ends_at)
                if not timed:
                    await self._mark_remaining(
                        job.id,
                        account_id,
                        ResultStatus.CANCELLED,
                        "Операция остановлена",
                        "cancelled",
                    )
        except Exception as exc:
            await self._mark_remaining(
                job.id, account_id, ResultStatus.FAILED, safe_error(exc), error_kind(exc)
            )
            async with self.db.session() as session:
                account = await session.get(TelegramAccount, account_id)
                if account:
                    account.last_error, account.last_error_at = safe_error(exc), utcnow()
                    account.health_ok = False
                    if isinstance(exc, AUTH_ERRORS):
                        account.status = AccountStatus.QUARANTINED
                        account.error_streak += 1

    async def _mark_remaining(self, job_id, account_id, status, message, kind):
        async with self.db.session() as session:
            query = update(AccountJobResult).where(
                AccountJobResult.job_id == job_id, AccountJobResult.state != "done"
            )
            if account_id is not None:
                query = query.where(AccountJobResult.account_id == account_id)
            await session.execute(
                query.values(
                    status=status,
                    state="done",
                    error=message,
                    error_kind=kind,
                    finished_at=utcnow(),
                )
            )

    async def _finalize(self, job_id, cancelled=False):
        async with self.db.session() as session:
            job = await session.get(Job, job_id, with_for_update=True)
            if not job or (job.finished_at and job.status not in ACTIVE_JOBS):
                return
            cancelled = cancelled or job.cancel_requested
            timed = job.mode == JobMode.TEST and job.ends_at and utcnow() >= aware(job.ends_at)
            rows = await job_results(session, job_id)
            for row in rows:
                if row.state != "done":
                    if cancelled:
                        row.status, row.error_kind, row.error = (
                            ResultStatus.CANCELLED,
                            "cancelled",
                            "Остановлено пользователем",
                        )
                    elif timed:
                        row.status = (
                            ResultStatus.COMPLETED
                            if row.full_scan_complete
                            else ResultStatus.PARTIAL
                        )
                        row.error_kind = "deadline"
                        row.error = (
                            None
                            if row.full_scan_complete
                            else "Время теста истекло до завершения полного прохода"
                        )
                    else:
                        row.status, row.error_kind, row.error = (
                            ResultStatus.FAILED,
                            "missing",
                            "Работник завершился без результата",
                        )
                    row.state, row.finished_at = "done", utcnow()
            success = sum(row.status == ResultStatus.COMPLETED for row in rows)
            if cancelled:
                job.status = JobStatus.CANCELLED
            elif rows and len(rows) == job.expected_results and success == job.expected_results:
                job.status = JobStatus.COMPLETED
            elif success or any(row.viewed_posts for row in rows):
                job.status = JobStatus.PARTIAL
            else:
                job.status = JobStatus.FAILED
            job.finished_at = utcnow()
            job.retention_until = job.finished_at + timedelta(
                days=self.settings.report_retention_days
            )
            job.summary = {
                "expected": job.expected_results,
                "recorded": len(rows),
                "completed": success,
                "reason": "deadline" if timed and not cancelled else job.stop_reason,
            }
            await session.flush()
            job.summary = dict(job.summary, channels=await aggregate_job(session, job_id))


class PermanentWorker:
    def __init__(self, settings, db, runner):
        self.settings, self.db, self.runner = settings, db, runner

    async def tick(self):
        if await paused(self.db):
            return
        async with self.db.session() as session:
            channels = list(
                (
                    await session.scalars(
                        select(Channel).where(
                            Channel.permanent.is_(True),
                            Channel.active.is_(True),
                            Channel.archived_at.is_(None),
                        )
                    )
                ).all()
            )
        for channel in channels:
            if (
                channel.last_cycle_at
                and (utcnow() - aware(channel.last_cycle_at)).total_seconds()
                < channel.check_interval_seconds
            ):
                continue
            try:
                job = await self.runner.create_from_channel_ids(
                    [channel.id],
                    mode=JobMode.PERMANENT_CYCLE,
                    created_by=channel.created_by,
                    automatic=True,
                )
            except ValueError:
                continue
            async with self.db.session() as session:
                (await session.get(Channel, channel.id)).last_cycle_at = utcnow()
            self.runner.launch(job.id)
