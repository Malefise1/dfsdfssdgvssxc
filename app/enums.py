from __future__ import annotations

from enum import StrEnum


class OperatorRole(StrEnum):
    MAIN_ADMIN = "main_admin"
    MANAGER = "manager"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    QUARANTINED = "quarantined"


class JobMode(StrEnum):
    MANUAL_FULL = "manual_full"
    TEST = "test"
    PERMANENT_CYCLE = "permanent_cycle"
    SCHEDULED_FULL = "scheduled_full"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ResultStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProxyProtocol(StrEnum):
    HTTP = "http"
    SOCKS5 = "socks5"


class ScheduleKind(StrEnum):
    ONCE = "once"
    INTERVAL = "interval"
