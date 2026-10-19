"""Offline database check. Never connect to Telegram or print session/account data."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.models import TelegramAccount  # noqa: E402
from app.security import SecretBox  # noqa: E402


async def snapshot(db, box):
    digest = hashlib.sha256()
    count = readable = 0
    async with db.session() as session:
        rows = (
            await session.execute(select(TelegramAccount.__table__).order_by(TelegramAccount.id))
        ).mappings()
        for row in rows:
            digest.update(
                json.dumps(dict(row), sort_keys=True, default=str, ensure_ascii=True).encode()
            )
            digest.update(b"\n")
            count += 1
            if row["archived_at"] is None and row["encrypted_session"]:
                box.decrypt(row["encrypted_session"])
                readable += 1
    return {"accounts": count, "readable_sessions": readable, "sha256": digest.hexdigest()}


async def main():
    settings = get_settings()
    db = Database(settings)
    try:
        await db.acquire_instance()
        await db.check_schema()
        result = await snapshot(db, SecretBox(settings.session_encryption_key))
        print(json.dumps(result))
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(
            f"Проверка аккаунтов не пройдена: {type(exc).__name__}. Изменений не внесено.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
