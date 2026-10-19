import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.enums import JobMode, JobStatus, ResultStatus, ScheduleKind
from app.models import AccountJobResult, Channel, Job, Schedule, TelegramAccount, utcnow
from app.services.control import paused
from app.services.jobs import JobRunner
from app.services.maintenance import ScheduleWorker
from app.services.reports import ReportService
from app.telegram.viewer import ViewOutcome


class Runtime:
    def __init__(self, db):
        self.db = db
        self.activity = {}
        self.stop_count = 0

    @asynccontextmanager
    async def connection(self, id_, purpose, cancel=None):
        async with self.db.session() as session:
            account = await session.get(TelegramAccount, id_)
        if account.encrypted_session == "broken":
            raise ValueError("broken session")
        yield object(), account, None

    async def stop_all(self):
        self.stop_count += 1


@pytest.fixture
async def runner(settings, db, seeded):
    value = JobRunner(settings, db, seeded.box, ReportService(settings, db), Runtime(db))
    yield value
    await value.shutdown()


async def create(runner, seeded, mode=JobMode.MANUAL_FULL):
    return await runner.create_from_channel_ids(
        [r.id for r in seeded.channels],
        mode=mode,
        created_by=1,
        duration_minutes=1 if mode == JobMode.TEST else None,
    )


async def finish(runner, id_):
    runner.launch(id_)
    task = runner._tasks[id_]
    await asyncio.wait_for(task, 3)


async def fake_view(client, username, *, start_after_id, on_progress, on_resolve=None, **kwargs):
    if on_resolve:
        await on_resolve(1000 + int(username[-1]))
    await on_progress(start_after_id + 10, 2)
    return ViewOutcome(2, 2, start_after_id + 10, username, False, 1000 + int(username[-1]))


async def test_planned_results_include_broken_session(runner, db, seeded, monkeypatch):
    monkeypatch.setattr("app.services.jobs.view_public_channel", fake_view)
    job = await create(runner, seeded)
    async with db.session() as session:
        (await session.get(TelegramAccount, seeded.accounts[0].id)).encrypted_session = "broken"
        planned = list(
            (
                await session.scalars(
                    select(AccountJobResult).where(AccountJobResult.job_id == job.id)
                )
            ).all()
        )
    assert len(planned) == 4 and all(r.state == "queued" for r in planned)
    await finish(runner, job.id)
    async with db.session() as session:
        stored = await session.get(Job, job.id)
        rows = list(
            (
                await session.scalars(
                    select(AccountJobResult).where(AccountJobResult.job_id == job.id)
                )
            ).all()
        )
    assert stored.status == JobStatus.PARTIAL
    assert sum(r.status == ResultStatus.FAILED for r in rows) == 2
    assert stored.summary["expected"] == stored.summary["recorded"] == 4


async def test_full_stop_survives_restart_and_blocks_schedules(runner, db, seeded, settings):
    _ = await create(runner, seeded)
    async with db.session() as session:
        session.add(
            Schedule(
                channel_id=seeded.channels[0].id,
                kind=ScheduleKind.ONCE,
                run_at=utcnow() - timedelta(minutes=1),
                created_by=1,
                enabled=True,
            )
        )
    await runner.stop_all(1)
    restarted = JobRunner(settings, db, seeded.box, runner.reports, runner.runtime)
    assert await paused(db)
    assert await restarted.resume_incomplete() == 0
    await ScheduleWorker(db, restarted).tick()
    async with db.session() as session:
        jobs = list((await session.scalars(select(Job))).all())
        assert len(jobs) == 1 and jobs[0].status == JobStatus.CANCELLED
        assert jobs[0].cancel_requested
        assert (await session.scalar(select(Schedule))).enabled


async def test_test_mode_visits_all_channels_and_deadline_is_normal(
    runner, db, seeded, monkeypatch
):
    visited = []

    async def visit(*args, **kwargs):
        visited.append(args[1])
        return await fake_view(*args, **kwargs)

    monkeypatch.setattr("app.services.jobs.view_public_channel", visit)
    job = await create(runner, seeded, JobMode.TEST)
    async with db.session() as session:
        (await session.get(Job, job.id)).ends_at = utcnow() + timedelta(seconds=0.4)
    await finish(runner, job.id)
    async with db.session() as session:
        stored = await session.get(Job, job.id)
    assert set(visited) == {"channel1", "channel2"}
    assert stored.status == JobStatus.COMPLETED and stored.summary["reason"] == "deadline"


async def test_graceful_restart_keeps_permanent_and_progress(
    runner, db, seeded, settings, monkeypatch
):
    checkpoint = asyncio.Event()

    async def waiting(*args, on_progress, on_resolve=None, **kwargs):
        await on_progress(123, 3)
        checkpoint.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("app.services.jobs.view_public_channel", waiting)
    job = await create(runner, seeded, JobMode.PERMANENT_CYCLE)
    runner.launch(job.id)
    await asyncio.wait_for(checkpoint.wait(), 2)
    await runner.shutdown()
    async with db.session() as session:
        stored = await session.get(Job, job.id)
        rows = list(
            (
                await session.scalars(
                    select(AccountJobResult).where(AccountJobResult.job_id == job.id)
                )
            ).all()
        )
        assert stored.status == JobStatus.PENDING and not stored.cancel_requested
        assert any(r.last_message_id == 123 for r in rows)
        assert all(c.active and c.permanent for c in (await session.scalars(select(Channel))).all())
    assert not await paused(db)
    cursors = []

    async def resumed(*args, **kwargs):
        cursors.append(kwargs["start_after_id"])
        return await fake_view(*args, **kwargs)

    monkeypatch.setattr("app.services.jobs.view_public_channel", resumed)
    restarted = JobRunner(settings, db, seeded.box, runner.reports, runner.runtime)
    assert await restarted.resume_incomplete() == 1
    await asyncio.wait_for(asyncio.gather(*list(restarted._tasks.values())), 3)
    assert 123 in cursors
    await restarted.shutdown()


async def test_schedule_creation_and_advance_are_atomic(runner, db, seeded):
    async with db.session() as session:
        row = Schedule(
            channel_id=seeded.channels[0].id,
            kind=ScheduleKind.ONCE,
            run_at=utcnow() - timedelta(minutes=1),
            created_by=1,
            enabled=True,
        )
        session.add(row)
    job = await runner.create_from_channel_ids(
        [row.channel_id],
        mode=JobMode.SCHEDULED_FULL,
        created_by=1,
        automatic=True,
        schedule_id=row.id,
    )
    with pytest.raises(ValueError):
        await runner.create_from_channel_ids(
            [row.channel_id],
            mode=JobMode.SCHEDULED_FULL,
            created_by=1,
            automatic=True,
            schedule_id=row.id,
        )
    async with db.session() as session:
        saved = await session.get(Schedule, row.id)
        assert not saved.enabled and saved.last_job_id == job.id


@pytest.mark.parametrize("minutes", [0, -1, 10081])
async def test_bounds_same_for_all_entry_points(runner, seeded, minutes):
    with pytest.raises(ValueError):
        await runner.create_from_channel_ids(
            [seeded.channels[0].id], mode=JobMode.TEST, created_by=1, duration_minutes=minutes
        )


async def test_manager_channel_limit_enforced_in_runner(runner, db, seeded):
    from app.services.control import put_setting

    await put_setting(db, "manager_channels:2", {"ids": [seeded.channels[0].id]})
    with pytest.raises(ValueError):
        await runner.create_from_channel_ids(
            [seeded.channels[1].id], mode=JobMode.MANUAL_FULL, created_by=2
        )
    job = await runner.create_from_channel_ids(
        [seeded.channels[0].id], mode=JobMode.MANUAL_FULL, created_by=2
    )
    assert job.expected_results == 2
