from __future__ import annotations

from contextlib import asynccontextmanager, suppress

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.enums import OperatorRole
from app.migrations import SCHEMA_VERSION, migrate
from app.models import AuditEvent, Operator, SchemaMigration


class Database:
    def __init__(self, settings):
        self.engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            hide_parameters=True,
        )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self._instance_connection = None

    @asynccontextmanager
    async def session(self):
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def check_schema(self):
        async with self.session() as session:
            version = await session.scalar(
                select(SchemaMigration.version).order_by(SchemaMigration.version.desc()).limit(1)
            )
            if version != SCHEMA_VERSION:
                raise RuntimeError(
                    "Нужна миграция: python manage.py migrate (сначала резервная копия)"
                )

    async def initialize(self, main_admin_id, retention_days=30):
        await migrate(self.engine, retention_days)
        await self.sync_owner(main_admin_id)

    async def sync_owner(self, main_admin_id):
        async with self.session() as session:
            old = list(
                (
                    await session.scalars(
                        select(Operator).where(
                            Operator.role == OperatorRole.MAIN_ADMIN,
                            Operator.telegram_id != main_admin_id,
                            Operator.active.is_(True),
                        )
                    )
                ).all()
            )
            for operator in old:
                operator.active = False
                operator.role = OperatorRole.MANAGER
                session.add(
                    AuditEvent(
                        actor_id=main_admin_id,
                        action="owner.revoked",
                        target=str(operator.telegram_id),
                    )
                )
            owner = await session.get(Operator, main_admin_id)
            if owner is None:
                owner = Operator(
                    telegram_id=main_admin_id, role=OperatorRole.MAIN_ADMIN, active=True
                )
                session.add(owner)
                session.add(
                    AuditEvent(
                        actor_id=main_admin_id, action="owner.assigned", target=str(main_admin_id)
                    )
                )
            else:
                owner.role = OperatorRole.MAIN_ADMIN
                owner.active = True

    async def acquire_instance(self):
        conn = await self.engine.connect()
        if not await conn.scalar(text("SELECT pg_try_advisory_lock(781263011)")):
            await conn.close()
            raise RuntimeError("Уже работает другой процесс этого проекта с этой базой")
        self._instance_connection = conn

    async def close(self):
        if self._instance_connection is not None:
            with suppress(Exception):
                await self._instance_connection.execute(
                    text("SELECT pg_advisory_unlock(781263011)")
                )
            await self._instance_connection.close()
            self._instance_connection = None
        await self.engine.dispose()
