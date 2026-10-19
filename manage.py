from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet


def configure(path):
    from dotenv import dotenv_values

    from app.config import Settings

    if not path.exists():
        raise ValueError("Сначала создайте файл настроек из .env.example или запустите установщик")
    data = dict(dotenv_values(path))
    print("Пустой ввод сохраняет текущее значение. Секреты не отображаются.")
    for key, label, hidden in [
        ("BOT_TOKEN", "Токен бота", True),
        ("MAIN_ADMIN_ID", "Telegram ID владельца", False),
        ("API_ID", "Telegram API ID", False),
        ("API_HASH", "Telegram API HASH", True),
    ]:
        value = (getpass.getpass(label + ": ") if hidden else input(label + ": ")).strip()
        if value:
            data[key] = value
    if not data.get("SESSION_ENCRYPTION_KEY"):
        data["SESSION_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    # Validate before replacing the existing config, without showing input values.
    Settings(_env_file=None, **{k.lower(): v for k, v in data.items() if v is not None})
    stat = path.stat()
    fd, name = tempfile.mkstemp(prefix="config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            for key, value in data.items():
                if value is not None:
                    stream.write(key + "=" + json.dumps(str(value), ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, stat.st_mode & 0o640)
        if os.geteuid() == 0:
            os.chown(name, stat.st_uid, stat.st_gid)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    print("[OK] Настройки проверены и сохранены; существующий ключ шифрования сохранён.")


async def command(args):
    from app.config import get_settings
    from app.db import Database
    from app.security import SecretBox
    from app.services.accounts import AccountService
    from app.services.maintenance import BackupWorker
    from app.services.runtime import AccountRuntime

    settings = get_settings()
    if args.command == "backup":
        settings.ensure_directories()
        print(await BackupWorker(settings).create_backup())
        return
    if args.command == "verify-backup":
        await BackupWorker(settings).verify(args.file)
        print("[OK] Каталог копии читается. Для полного теста: scripts/verify_restore.py")
        return
    db = Database(settings)
    try:
        await db.acquire_instance()  # migrations/import only with bot stopped
        if args.command == "migrate":
            settings.ensure_directories()
            await db.initialize(settings.main_admin_id, settings.report_retention_days)
            print(
                "[OK] Миграция завершена. После обновления проверьте настройки и снимите общую паузу в боте."
            )
        elif args.command == "import-local":
            from app.services.imports import parse_upload

            await db.check_schema()
            settings.ensure_directories()
            if args.file.stat().st_size > settings.max_upload_bytes:
                raise ValueError("Файл слишком большой")
            items = parse_upload(args.file.name, args.file.read_bytes(), settings)
            if any(item.kind != "session" for item in items):
                raise ValueError(
                    "Локальный импорт принимает только готовые сессии; номера добавляйте через QR в боте"
                )
            secrets = SecretBox(settings.session_encryption_key)
            runtime = AccountRuntime(settings, db, secrets)
            service = AccountService(settings, db, secrets, runtime)
            for index, item in enumerate(items, 1):
                try:
                    row, added = await service.import_session(item.value, settings.main_admin_id)
                    print(
                        f"{index}: №{row.display_number} — " + ("добавлен" if added else "уже есть")
                    )
                except Exception as exc:
                    print(f"{index}: ошибка {type(exc).__name__}")
                finally:
                    item.value = ""
    finally:
        await db.close()


def main():
    parser = argparse.ArgumentParser(description="Channel Viewer 2.0")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate-key", help="Новый ключ; не заменяйте им ключ работающей базы")
    sub.add_parser("migrate", help="Миграция при остановленном боте; сначала резервная копия")
    sub.add_parser("backup", help="Атомарная резервная копия PostgreSQL")
    for name in ("verify-backup", "import-local"):
        sub.add_parser(name).add_argument("file", type=Path)
    config = sub.add_parser("configure", help="Ввод настроек без секретов в истории shell")
    config.add_argument("file", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "generate-key":
            print(Fernet.generate_key().decode())
        elif args.command == "configure":
            configure(args.file)
        else:
            asyncio.run(command(args))
    except (Exception, KeyboardInterrupt) as exc:
        print(
            f"[ERROR] {type(exc).__name__}. Операция не завершена; проверьте настройки, доступ и состояние службы."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
