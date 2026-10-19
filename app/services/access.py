"""Optional channel scope for managers. Absent setting preserves the original all-channel access."""

from sqlalchemy import select

from app.models import JobChannel
from app.services.control import setting


async def channel_scope(settings, db, actor):
    if actor == settings.main_admin_id:
        return None
    value = await setting(db, f"manager_channels:{actor}", {})
    ids = value.get("ids")
    return None if ids is None else set(int(id_) for id_ in ids)


async def require_channels(settings, db, actor, ids):
    scope = await channel_scope(settings, db, actor)
    if scope is not None and not set(ids) <= scope:
        raise ValueError(
            "Этот канал не включён в доступ менеджера. Обратитесь к главному администратору."
        )


async def allowed_job(settings, db, actor, job_id):
    scope = await channel_scope(settings, db, actor)
    if scope is None:
        return True
    async with db.session() as session:
        ids = set(
            (
                await session.scalars(
                    select(JobChannel.channel_id).where(JobChannel.job_id == job_id)
                )
            ).all()
        )
    return bool(ids) and ids <= scope
