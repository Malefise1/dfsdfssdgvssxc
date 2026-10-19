"""Read-only diagnostics: no mkdir, schema changes, writes, or role synchronization."""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
import shutil
import sys
from pathlib import Path

from sqlalchemy import select, text


async def run():
    errors = 0
    print("[OK] Python " + sys.version.split()[0])
    if sys.version_info < (3, 12):  # noqa: UP036 - may run before dependencies are installed
        print("[ERROR] Для этой сборки требуется Python 3.12+")
        errors += 1
    for name in (
        "aiogram",
        "Telethon",
        "SQLAlchemy",
        "asyncpg",
        "cryptography",
        "qrcode",
        "openpyxl",
    ):
        try:
            print(f"[OK] {name} {importlib.metadata.version(name)}")
        except importlib.metadata.PackageNotFoundError:
            print(f"[ERROR] Не установлено: {name}")
            errors += 1
    try:
        from app.config import get_settings
        from app.db import Database
        from app.models import Proxy, TelegramAccount
        from app.security import SecretBox

        settings = get_settings()
        box = SecretBox(settings.session_encryption_key)
        env = Path(os.environ.get("TGVIEWER_ENV_FILE", ".env"))
        if env.exists() and env.stat().st_mode & 0o007:
            print("[ERROR] Файл настроек доступен посторонним пользователям")
            errors += 1
        db = Database(settings)
        try:
            await db.check_schema()
            async with db.session() as session:
                version = await session.scalar(text("SHOW server_version"))
                values = list(
                    (
                        await session.scalars(
                            select(TelegramAccount.encrypted_session)
                            .where(TelegramAccount.archived_at.is_(None))
                            .limit(5)
                        )
                    ).all()
                )
                values += list(
                    (await session.scalars(select(Proxy.encrypted_password).limit(5))).all()
                )
            for value in values:
                box.decrypt(value)
            print(f"[OK] PostgreSQL {version}; схема v2; выборочная проверка расшифровки")
        finally:
            await db.close()
        for path in (
            settings.data_dir,
            settings.logs_dir,
            settings.reports_dir,
            settings.backups_dir,
        ):
            if not path.is_dir() or not os.access(path, os.W_OK | os.X_OK):
                print(f"[ERROR] Каталог недоступен для записи: {path}")
                errors += 1
            elif path.stat().st_mode & 0o077:
                print(f"[ERROR] Слишком широкие права каталога: {path}")
                errors += 1
            else:
                print(f"[OK] Каталог {path.name}: доступ и права")
        for name in ("pg_dump", "pg_restore"):
            if not shutil.which(name):
                print(f"[ERROR] Не найден {name}")
                errors += 1
            else:
                print(f"[OK] Найден {name}")
    except Exception as exc:
        # Never print ValidationError/SQLAlchemy connection strings or arbitrary exception values.
        print(
            f"[ERROR] Настройки/БД: {type(exc).__name__}. Проверьте конфигурацию, ключ и выполненную миграцию."
        )
        errors += 1
    print("[OK] Проверка завершена" if not errors else f"[ERROR] Проблем: {errors}")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
