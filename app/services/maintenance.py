from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url

from app.enums import AccountStatus, JobMode, ScheduleKind
from app.errors import safe_error
from app.local import file_lock
from app.models import Channel, Job, Operator, Schedule, TelegramAccount, utcnow
from app.services.control import audit, aware, paused, put_setting, setting

logger = logging.getLogger(__name__)


async def periodic(name, interval, function, stop, health):
    while not stop.is_set():
        try:
            await function()
            health[name] = {"ok": True, "at": utcnow().isoformat()}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            health[name] = {"ok": False, "at": utcnow().isoformat(), "error": safe_error(exc)}
            logger.error("Worker %s: %s", name, type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except TimeoutError:
            pass


class ScheduleWorker:
    def __init__(self, db, runner):
        self.db, self.runner = db, runner

    async def tick(self):
        if await paused(self.db):
            return
        async with self.db.session() as session:
            due = list(
                (
                    await session.scalars(
                        select(Schedule)
                        .join(Channel)
                        .where(
                            Schedule.enabled.is_(True),
                            Schedule.run_at <= utcnow(),
                            Channel.archived_at.is_(None),
                        )
                    )
                ).all()
            )
        for schedule in due:
            try:
                job = await self.runner.create_from_channel_ids(
                    [schedule.channel_id],
                    mode=JobMode.SCHEDULED_FULL,
                    created_by=schedule.created_by,
                    automatic=True,
                    schedule_id=schedule.id,
                )
            except ValueError as exc:
                async with self.db.session() as session:
                    row = await session.get(Schedule, schedule.id)
                    if row:
                        row.last_error = str(exc)[:500]
                continue
            self.runner.launch(job.id)


class ScheduleService:
    def __init__(self, db):
        self.db = db

    async def save(self, channel_id, actor_id, *, interval=None, run_at=None, schedule_id=None):
        if interval is not None and not 60 <= interval <= 525600:
            raise ValueError("Интервал: от 60 до 525600 минут")
        if run_at is not None and not utcnow() < aware(run_at) <= utcnow() + timedelta(days=366):
            raise ValueError("Укажите будущее время в пределах года")
        if interval is None and run_at is None:
            raise ValueError("Не задано время запуска")
        async with self.db.session() as session:
            channel = await session.get(Channel, channel_id)
            if not channel or channel.archived_at:
                raise ValueError("Канал недоступен")
            row = await session.get(Schedule, schedule_id) if schedule_id else None
            if schedule_id and not row:
                raise ValueError("Расписание удалено")
            if row is None:
                row = Schedule(channel_id=channel_id, created_by=actor_id)
                session.add(row)
            row.kind = ScheduleKind.INTERVAL if interval is not None else ScheduleKind.ONCE
            row.interval_minutes = interval
            row.run_at = run_at or (utcnow() + timedelta(minutes=interval))
            row.enabled, row.last_error = True, None
            await session.flush()
        await audit(
            self.db, actor_id, "schedule.saved", row.id, channel=channel_id, interval=interval
        )
        return row

    async def toggle(self, schedule_id, actor_id):
        async with self.db.session() as session:
            row = await session.get(Schedule, schedule_id)
            if not row:
                raise ValueError("Расписание удалено")
            row.enabled = not row.enabled
            if row.enabled and aware(row.run_at) < utcnow():
                row.run_at = utcnow() + timedelta(seconds=30)
            enabled = row.enabled
        await audit(self.db, actor_id, "schedule.enabled", schedule_id, enabled=enabled)
        return enabled

    async def delete(self, schedule_id, actor_id):
        async with self.db.session() as session:
            await session.execute(delete(Schedule).where(Schedule.id == schedule_id))
        await audit(self.db, actor_id, "schedule.deleted", schedule_id)


class BackupWorker:
    def __init__(self, settings):
        self.settings = settings
        self._lock = asyncio.Lock()

    @property
    def state_path(self):
        return self.settings.data_dir / "backup-state.json"

    def state(self):
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _state(self, data):
        path = self.state_path.with_name("backup-state-" + uuid.uuid4().hex + ".tmp")
        with path.open("w", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            stream.write(json.dumps(data))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path, self.state_path)

    def instance_tag(self):
        url = make_url(self.settings.database_url)
        return hashlib.sha256(f"{url.host}|{url.port or 5432}|{url.database}".encode()).hexdigest()[
            :10
        ]

    async def tick(self):
        state = self.state()
        if self.settings.backup_mirror_dir and state.get("file") and not state.get("mirror_ok"):
            async with self._lock:
                with file_lock(self.settings.data_dir / "backup.lock"):
                    await self.mirror(self.settings.backups_dir / state["file"])
                    state.update(mirror_ok=True, mirror_at=utcnow().isoformat())
                    state.pop("error", None)
                    self._state(state)
        latest = self.state().get("success_at")
        if not latest or utcnow() - datetime.fromisoformat(latest) >= timedelta(
            days=self.settings.backup_interval_days
        ):
            await self.create_backup()

    async def create_backup(self):
        if self._lock.locked():
            raise ValueError("Резервное копирование уже выполняется")
        async with self._lock:
            with file_lock(self.settings.data_dir / "backup.lock"):
                pg_dump, pg_restore = shutil.which("pg_dump"), shutil.which("pg_restore")
                if not pg_dump or not pg_restore:
                    raise RuntimeError("Нужны pg_dump и pg_restore")
                url = make_url(self.settings.database_url)
                name = f"tgviewer_{self.instance_tag()}_{utcnow():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.dump"
                destination = self.settings.backups_dir / name
                temporary = destination.with_suffix(".partial")
                env = {
                    key: value
                    for key, value in os.environ.items()
                    if key in ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL")
                }
                env["PGPASSWORD"] = url.password or ""
                env["PGCONNECT_TIMEOUT"] = "15"
                for key, value in url.query.items():
                    if key == "ssl":
                        env["PGSSLMODE"] = str(value)
                    elif key in ("sslmode", "sslrootcert", "sslcert", "sslkey"):
                        env["PG" + key.upper()] = str(value)
                command = [
                    pg_dump,
                    "--format=custom",
                    "--no-owner",
                    "--file",
                    str(temporary),
                    "--host",
                    url.host or "127.0.0.1",
                    "--port",
                    str(url.port or 5432),
                    "--username",
                    url.username or "postgres",
                    url.database,
                ]
                process = None
                try:
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        env=env,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, _ = await asyncio.wait_for(
                        process.communicate(), self.settings.backup_timeout_seconds
                    )
                    if process.returncode:
                        raise RuntimeError(
                            "pg_dump завершился с ошибкой; проверьте доступ к БД и свободное место"
                        )
                    temporary.chmod(0o600)
                    with temporary.open("rb") as stream:
                        os.fsync(stream.fileno())
                    await self.verify(temporary)
                    os.replace(temporary, destination)
                    self._state(
                        {
                            "success_at": utcnow().isoformat(),
                            "file": destination.name,
                            "catalog_verified": True,
                        }
                    )
                    backups = sorted(self.settings.backups_dir.glob("tgviewer_*.dump"))
                    for old in backups[: -self.settings.backup_keep_count]:
                        old.unlink()
                    if self.settings.backup_mirror_dir:
                        await self.mirror(destination)
                        state = self.state()
                        state.update(mirror_ok=True, mirror_at=utcnow().isoformat())
                        self._state(state)
                    return destination
                except BaseException as exc:
                    if process and process.returncode is None:
                        process.kill()
                        await process.communicate()
                    temporary.unlink(missing_ok=True)
                    data = self.state()
                    data.update(error=safe_error(exc), failed_at=utcnow().isoformat())
                    self._state(data)
                    raise

    async def mirror(self, path):
        """Optional encrypted mirror to a separately provisioned mount; private age key stays off-server."""
        directory = self.settings.backup_mirror_dir
        if not directory:
            return
        binary = shutil.which("age")
        if not binary:
            raise RuntimeError("Для зашифрованной внешней копии установите age")
        if not directory.is_dir():
            raise RuntimeError("Каталог внешней копии не доступен")
        target = directory / (path.name + ".age")
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".partial")
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                binary,
                "--recipient",
                self.settings.backup_age_recipient,
                "--output",
                str(temporary),
                str(path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), self.settings.backup_timeout_seconds)
            if process.returncode or not temporary.is_file() or temporary.stat().st_size < 100:
                raise RuntimeError("Зашифрованная внешняя копия не создана")
            temporary.chmod(0o600)
            os.replace(temporary, target)
            copies = sorted(directory.glob(f"tgviewer_{self.instance_tag()}_*.dump.age"))
            for old in copies[: -self.settings.backup_keep_count]:
                old.unlink()
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.communicate()
            temporary.unlink(missing_ok=True)

    async def verify(self, path):
        process = await asyncio.create_subprocess_exec(
            shutil.which("pg_restore") or "pg_restore",
            "--list",
            str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(process.communicate(), 60)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.communicate()
            raise
        if process.returncode or path.stat().st_size < 100:
            raise RuntimeError("Проверка каталога резервной копии не пройдена")


class RetentionWorker:
    def __init__(self, settings, db, bot):
        self.settings, self.db, self.bot = settings, db, bot

    async def keep(self, job_id, actor_id=0):
        async with self.db.session() as session:
            job = await session.get(Job, job_id)
            if not job or not job.finished_at:
                return False
            job.retention_until = max(aware(job.retention_until) or utcnow(), utcnow()) + timedelta(
                days=30
            )
            job.deletion_reminded_at, job.deletion_approved = None, False
        await audit(self.db, actor_id, "report.retention_extended", job_id)
        return True

    async def delete_confirmed(self, job_id, actor_id=0):
        async with self.db.session() as session:
            job = await session.get(Job, job_id)
            if not job or not job.finished_at:
                return False
            job.deletion_approved = True
        await audit(self.db, actor_id, "report.deletion_approved", job_id)
        await self.cleanup()
        return True

    async def cleanup(self):
        async with self.db.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(Job).where(
                            Job.deletion_approved.is_(True),
                            Job.retention_until <= utcnow(),
                            Job.finished_at.is_not(None),
                        )
                    )
                ).all()
            )
            ids = [job.id for job in rows]
            legacy_paths = [job.report_path for job in rows if job.report_path]
            for job in rows:
                await session.delete(job)
        root = self.settings.reports_dir.resolve()
        for stored in legacy_paths:
            candidate = Path(stored).resolve()
            if candidate.parent == root and candidate.suffix == ".xlsx":
                candidate.unlink(missing_ok=True)
        for job_id in ids:
            for path in root.glob(f"report_{job_id}_*.xlsx"):
                if path.resolve().parent == root:
                    path.unlink(missing_ok=True)
        # Temporary exports that have no Job row have their own retention period.
        threshold = utcnow().timestamp() - self.settings.report_retention_days * 86400
        for prefix in ("daily_", "problem_accounts_", "subscriptions_", "import_"):
            for path in root.glob(prefix + "*.xlsx"):
                if path.stat().st_mtime < threshold:
                    path.unlink(missing_ok=True)

    async def send_reminders(self):
        await self.cleanup()
        async with self.db.session() as session:
            jobs = list(
                (
                    await session.scalars(
                        select(Job)
                        .where(
                            Job.retention_until
                            <= utcnow() + timedelta(days=self.settings.report_reminder_days),
                            Job.deletion_approved.is_(False),
                            Job.deletion_reminded_at.is_(None),
                            Job.finished_at.is_not(None),
                        )
                        .order_by(Job.retention_until)
                    )
                ).all()
            )
            ids = list(
                (
                    await session.scalars(
                        select(Operator.telegram_id).where(Operator.active.is_(True))
                    )
                ).all()
            )
        if not jobs:
            return
        day = utcnow().astimezone(self.settings.tz).date().isoformat()
        delivered = True
        for operator in ids:
            key = f"retention_sent:{operator}"
            previous = await setting(self.db, key, {})
            if previous.get("day") == day:
                delivered = False
                continue
            try:
                await self.bot.send_message(
                    operator,
                    f"📁 Срок хранения подходит к концу у {len(jobs)} отчётов.\n"
                    "Удаление выполняется только после подтверждения владельцем и наступления срока. Откройте фильтр «Срок хранения».",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="Отчёты", callback_data="reports:due:0")]
                        ]
                    ),
                )
                await put_setting(self.db, key, {"day": day})
            except Exception:
                delivered = False
                logger.warning("Retention notification not delivered to operator %s", operator)
        if delivered and ids:
            async with self.db.session() as session:
                for job in jobs:
                    row = await session.get(Job, job.id)
                    if row:
                        row.deletion_reminded_at = utcnow()


class HealthWorker:
    def __init__(self, settings, db, accounts, proxies):
        self.settings, self.db, self.accounts, self.proxies = settings, db, accounts, proxies
        self.last_proxy_check = None

    async def tick(self):
        if await paused(self.db):
            return
        if not self.last_proxy_check or utcnow() - self.last_proxy_check >= timedelta(hours=1):
            await self.proxies.check_all()
            self.last_proxy_check = utcnow()
        async with self.db.session() as session:
            accounts = list(
                (
                    await session.scalars(
                        select(TelegramAccount)
                        .where(
                            TelegramAccount.archived_at.is_(None),
                            TelegramAccount.status == AccountStatus.ACTIVE,
                        )
                        .order_by(TelegramAccount.health_checked_at.asc().nullsfirst())
                    )
                ).all()
            )
        due = [
            row
            for row in accounts
            if not row.health_checked_at
            or utcnow() - aware(row.health_checked_at)
            >= timedelta(hours=self.settings.healthcheck_interval_hours)
        ]
        for row in due[:10]:
            if row.flood_until and aware(row.flood_until) > utcnow():
                continue
            try:
                await asyncio.wait_for(
                    self.accounts.check(row.id), self.settings.rpc_timeout_seconds + 15
                )
            except TimeoutError:
                logger.warning("Health check timed out for account %s", row.display_number)
