"""Explicit, additive PostgreSQL migrations; never run by doctor or normal startup."""

from sqlalchemy import text

from app.models import Base

SCHEMA_VERSION = 2
ADDITIONS = {
    "telegram_accounts": {
        "display_number": "INTEGER",
        "session_fingerprint": "VARCHAR(64)",
        "label": "VARCHAR(255) NOT NULL DEFAULT ''",
        "archived_at": "TIMESTAMPTZ",
        "health_checked_at": "TIMESTAMPTZ",
        "health_ok": "BOOLEAN",
        "flood_until": "TIMESTAMPTZ",
    },
    "channels": {"telegram_id": "BIGINT", "archived_at": "TIMESTAMPTZ"},
    "jobs": {
        "cancel_requested": "BOOLEAN NOT NULL DEFAULT FALSE",
        "stop_reason": "VARCHAR(100)",
        "expected_results": "INTEGER NOT NULL DEFAULT 0",
        "retention_until": "TIMESTAMPTZ",
        "deletion_approved": "BOOLEAN NOT NULL DEFAULT FALSE",
    },
    "account_job_results": {
        "state": "VARCHAR(32) NOT NULL DEFAULT 'done'",
        "account_number": "INTEGER NOT NULL DEFAULT 0",
        "account_phone": "VARCHAR(32) NOT NULL DEFAULT ''",
        "channel_title": "VARCHAR(255) NOT NULL DEFAULT ''",
        "channel_url": "VARCHAR(255) NOT NULL DEFAULT ''",
        "full_scan_complete": "BOOLEAN NOT NULL DEFAULT FALSE",
        "error_kind": "VARCHAR(64)",
        "updated_at": "TIMESTAMPTZ NOT NULL DEFAULT now()",
    },
    "schedules": {"last_job_id": "VARCHAR(36)", "last_error": "VARCHAR(500)"},
}


async def migrate(engine, retention_days: int = 30) -> bool:
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(781263012)"))
        await conn.run_sync(Base.metadata.create_all)
        current = await conn.scalar(text("SELECT max(version) FROM schema_migrations"))
        if current and current > SCHEMA_VERSION:
            raise RuntimeError("База создана более новой версией приложения; откат запрещён")
        if current == SCHEMA_VERSION:
            return False
        for table, columns in ADDITIONS.items():
            for name, definition in columns.items():
                await conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {definition}")
                )
        # Assign new numbers above the existing range; never overwrite an assigned number.
        await conn.execute(
            text("""WITH missing AS (
          SELECT id, row_number() OVER (ORDER BY id) AS n FROM telegram_accounts WHERE display_number IS NULL
        ), top AS (SELECT coalesce(max(display_number),0) AS n FROM telegram_accounts)
        UPDATE telegram_accounts a SET display_number=(missing.n+top.n)::integer
        FROM missing, top WHERE a.id=missing.id""")
        )
        await conn.execute(
            text("ALTER TABLE telegram_accounts ALTER COLUMN display_number SET NOT NULL")
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_telegram_accounts_display_number ON telegram_accounts(display_number)"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_session_fingerprint ON telegram_accounts(session_fingerprint)"
            )
        )
        await conn.execute(
            text("""UPDATE account_job_results r SET account_number=a.display_number,
            account_phone=a.phone FROM telegram_accounts a WHERE r.account_id=a.id AND r.account_number=0""")
        )
        await conn.execute(
            text("""UPDATE account_job_results r SET channel_title=c.title, channel_url=c.url
            FROM channels c WHERE r.channel_id=c.id AND r.channel_url=''""")
        )
        await conn.execute(
            text("""UPDATE jobs SET retention_until=finished_at+make_interval(days => :days)
            WHERE finished_at IS NOT NULL AND retention_until IS NULL"""),
            {"days": retention_days},
        )
        await conn.execute(
            text("""UPDATE jobs j SET expected_results=(SELECT count(*) FROM account_job_results r WHERE r.job_id=j.id)
            WHERE expected_results=0""")
        )
        await conn.execute(
            text("""UPDATE jobs SET summary=(coalesce(summary::jsonb,'{}'::jsonb)
            || jsonb_build_object('legacy_planning_unknown',true))::json""")
        )
        # The old version did not record the planned set. Do not silently replay legacy jobs.
        await conn.execute(
            text("""UPDATE jobs SET cancel_requested=TRUE, stop_reason='legacy_migration',
            status='CANCELLED', finished_at=now(), retention_until=now()+make_interval(days => :days)
            WHERE status IN ('PENDING','RUNNING')"""),
            {"days": retention_days},
        )
        await conn.execute(
            text("""INSERT INTO system_settings(key,value) VALUES ('global_pause',CAST(:value AS JSON))
            ON CONFLICT(key) DO UPDATE SET value=excluded.value"""),
            {"value": '{"paused":true}'},
        )
        await conn.execute(
            text("INSERT INTO schema_migrations(version,applied_at) VALUES (:v,now())"),
            {"v": SCHEMA_VERSION},
        )
    return True
