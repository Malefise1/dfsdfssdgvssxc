from datetime import UTC

from app.models import AuditEvent, SystemSetting


def aware(value):
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


async def setting(db, key, default=None):
    async with db.session() as session:
        row = await session.get(SystemSetting, key)
        return row.value if row else default


async def put_setting(db, key, value):
    async with db.session() as session:
        row = await session.get(SystemSetting, key)
        if row:
            row.value = value
        else:
            session.add(SystemSetting(key=key, value=value))


async def paused(db):
    return bool((await setting(db, "global_pause", {"paused": True})).get("paused", True))


async def audit(db, actor_id, action, target="", **detail):
    # Only explicit, non-secret fields are passed by callers.
    async with db.session() as session:
        session.add(AuditEvent(actor_id=actor_id, action=action, target=str(target), detail=detail))
