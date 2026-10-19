from __future__ import annotations

from sqlalchemy import delete, func, select

from app.enums import AccountStatus, JobStatus, OperatorRole
from app.models import (
    AccountJobResult,
    AccountProgress,
    Channel,
    Job,
    JobChannel,
    KnownUser,
    Operator,
    TelegramAccount,
    utcnow,
)


async def touch_known_user(session, telegram_id, username, full_name):
    row = await session.get(KnownUser, telegram_id)
    if row is None:
        row = KnownUser(telegram_id=telegram_id)
        session.add(row)
    row.username, row.full_name, row.last_seen_at = username, full_name[:255], utcnow()
    return row


async def get_operator(session, telegram_id):
    return await session.scalar(
        select(Operator).where(Operator.telegram_id == telegram_id, Operator.active.is_(True))
    )


async def list_operators(session):
    return list(
        (
            await session.scalars(
                select(Operator).where(Operator.active.is_(True)).order_by(Operator.telegram_id)
            )
        ).all()
    )


async def list_manager_candidates(session):
    ids = select(Operator.telegram_id).where(Operator.active.is_(True))
    return list(
        (
            await session.scalars(
                select(KnownUser)
                .where(KnownUser.telegram_id.not_in(ids))
                .order_by(KnownUser.last_seen_at.desc())
                .limit(50)
            )
        ).all()
    )


async def promote_manager(session, telegram_id):
    row = await session.get(Operator, telegram_id)
    if row is None:
        row = Operator(telegram_id=telegram_id, role=OperatorRole.MANAGER)
        session.add(row)
    elif row.role == OperatorRole.MAIN_ADMIN:
        raise ValueError("Нельзя менять владельца через назначение менеджера")
    row.active = True
    return row


async def remove_manager(session, telegram_id):
    row = await session.get(Operator, telegram_id)
    if not row or row.role == OperatorRole.MAIN_ADMIN:
        return False
    row.active = False
    return True


async def list_accounts(session, statuses=None):
    query = select(TelegramAccount).where(TelegramAccount.archived_at.is_(None))
    if statuses:
        query = query.where(TelegramAccount.status.in_(statuses))
    return list((await session.scalars(query.order_by(TelegramAccount.display_number))).all())


async def list_active_accounts(session):
    return await list_accounts(session, [AccountStatus.ACTIVE])


async def upsert_channel(session, username, url, created_by, permanent=False):
    username = username.lower()
    row = await session.scalar(
        select(Channel)
        .where(func.lower(Channel.username) == username)
        .order_by(Channel.id)
        .limit(1)
    )
    if row is None:
        row = Channel(
            username=username,
            url=f"https://t.me/{username}",
            created_by=created_by,
            permanent=permanent,
            active=True,
        )
        session.add(row)
        await session.flush()
    else:
        row.archived_at = None
        if permanent:
            row.permanent, row.active = True, True
    return row


async def create_job(session, mode, channel_ids, created_by, duration_minutes=None):
    row = Job(mode=mode, created_by=created_by, duration_minutes=duration_minutes)
    session.add(row)
    await session.flush()
    session.add_all(
        [JobChannel(job_id=row.id, channel_id=id_) for id_ in dict.fromkeys(channel_ids)]
    )
    return row


async def get_job_channels(session, job_id):
    return list(
        (
            await session.scalars(
                select(Channel)
                .join(JobChannel)
                .where(JobChannel.job_id == job_id)
                .order_by(Channel.id)
            )
        ).all()
    )


async def get_progress(session, account_id, channel_id):
    row = await session.scalar(
        select(AccountProgress).where(
            AccountProgress.account_id == account_id, AccountProgress.channel_id == channel_id
        )
    )
    if row is None:
        row = AccountProgress(account_id=account_id, channel_id=channel_id)
        session.add(row)
        await session.flush()
    return row


async def job_results(session, job_id):
    return list(
        (
            await session.scalars(
                select(AccountJobResult)
                .where(AccountJobResult.job_id == job_id)
                .order_by(AccountJobResult.channel_id, AccountJobResult.account_number)
            )
        ).all()
    )


async def aggregate_job(session, job_id):
    results = await job_results(session, job_id)
    groups = {}
    for r in results:
        row = groups.setdefault(
            r.channel_id,
            {
                "id": r.channel_id,
                "title": r.channel_title,
                "url": r.channel_url,
                "accounts_total": 0,
                "accounts_completed": 0,
                "available_posts": 0,
                "viewed_posts": 0,
            },
        )
        row["accounts_total"] += 1
        row["accounts_completed"] += int(r.status.value == "completed" and r.state == "done")
        row["available_posts"] = max(row["available_posts"], r.available_posts)
        row["viewed_posts"] += r.viewed_posts
    return list(groups.values())


async def has_running_job_for_channel(session, channel_id):
    return bool(
        await session.scalar(
            select(func.count())
            .select_from(Job)
            .join(JobChannel)
            .where(
                JobChannel.channel_id == channel_id,
                Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
                Job.cancel_requested.is_(False),
            )
        )
    )


async def latest_jobs(session, limit=20):
    return list(
        (await session.scalars(select(Job).order_by(Job.created_at.desc()).limit(limit))).all()
    )


async def delete_jobs(session, job_ids):
    result = await session.execute(
        delete(Job).where(Job.id.in_(job_ids), Job.finished_at.is_not(None))
    )
    return result.rowcount
