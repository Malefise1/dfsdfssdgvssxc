from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.enums import (
    AccountStatus,
    JobMode,
    JobStatus,
    OperatorRole,
    ProxyProtocol,
    ResultStatus,
    ScheduleKind,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class KnownUser(Base):
    __tablename__ = "known_users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(255), default="")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Operator(Base):
    __tablename__ = "operators"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[OperatorRole] = mapped_column(Enum(OperatorRole, name="operator_role"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    display_number: Mapped[int] = mapped_column(Integer, index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True)
    encrypted_session: Mapped[str] = mapped_column(Text)
    status: Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus, name="account_status"), default=AccountStatus.ACTIVE, index=True
    )
    error_streak: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_proxy_id: Mapped[int | None] = mapped_column(Integer)
    session_fingerprint: Mapped[str | None] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(255), default="")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health_ok: Mapped[bool | None] = mapped_column(Boolean)
    flood_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Proxy(Base):
    __tablename__ = "proxies"
    __table_args__ = (
        UniqueConstraint("protocol", "host", "port", "login", name="uq_proxy_endpoint"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    protocol: Mapped[ProxyProtocol] = mapped_column(Enum(ProxyProtocol, name="proxy_protocol"))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    login: Mapped[str] = mapped_column(String(255))
    encrypted_password: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_check_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    url: Mapped[str] = mapped_column(String(255))
    permanent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    check_interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    last_cycle_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccountChannelSubscription(Base):
    __tablename__ = "account_channel_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "channel_id",
            name="uq_account_channel_subscription",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_accounts.id", ondelete="CASCADE"),
        index=True,
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"),
        index=True,
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    subscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class AccountProgress(Base):
    __tablename__ = "account_progress"
    __table_args__ = (
        UniqueConstraint("account_id", "channel_id", name="uq_account_channel_progress"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_accounts.id", ondelete="CASCADE"), index=True
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    last_viewed_message_id: Mapped[int] = mapped_column(Integer, default=0)
    last_full_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_registered: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    mode: Mapped[JobMode] = mapped_column(Enum(JobMode, name="job_mode"), index=True)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status"), default=JobStatus.PENDING, index=True
    )
    created_by: Mapped[int] = mapped_column(BigInteger)
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    report_path: Mapped[str | None] = mapped_column(Text)
    deletion_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_reason: Mapped[str | None] = mapped_column(String(100))
    expected_results: Mapped[int] = mapped_column(Integer, default=0)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_approved: Mapped[bool] = mapped_column(Boolean, default=False)

    channels: Mapped[list[JobChannel]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class JobChannel(Base):
    __tablename__ = "job_channels"
    __table_args__ = (UniqueConstraint("job_id", "channel_id", name="uq_job_channel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )

    job: Mapped[Job] = relationship(back_populates="channels")


class AccountJobResult(Base):
    __tablename__ = "account_job_results"
    __table_args__ = (
        UniqueConstraint(
            "job_id", "account_id", "channel_id", name="uq_job_account_channel_result"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_accounts.id", ondelete="CASCADE"), index=True
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    proxy_id: Mapped[int | None] = mapped_column(ForeignKey("proxies.id", ondelete="SET NULL"))
    status: Mapped[ResultStatus] = mapped_column(Enum(ResultStatus, name="result_status"))
    available_posts: Mapped[int] = mapped_column(Integer, default=0)
    viewed_posts: Mapped[int] = mapped_column(Integer, default=0)
    last_message_id: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(32), default="queued")
    account_number: Mapped[int] = mapped_column(Integer, default=0)
    account_phone: Mapped[str] = mapped_column(String(32), default="")
    channel_title: Mapped[str] = mapped_column(String(255), default="")
    channel_url: Mapped[str] = mapped_column(String(255), default="")
    full_scan_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    error_kind: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[ScheduleKind] = mapped_column(Enum(ScheduleKind, name="schedule_kind"))
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    interval_minutes: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by: Mapped[int] = mapped_column(BigInteger)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_job_id: Mapped[str | None] = mapped_column(String(36))
    last_error: Mapped[str | None] = mapped_column(String(500))


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    actor_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(150), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    total: Mapped[int] = mapped_column(Integer)
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
