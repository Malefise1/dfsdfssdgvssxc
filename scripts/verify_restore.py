#!/usr/bin/env python3
"""Verify a dump by restoring to a newly created random test DB, never to a live DB."""

import argparse
import os
import secrets
import subprocess
from pathlib import Path


def run(args, **kwargs):
    return subprocess.run(["runuser", "-u", "postgres", "--", *args], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(
        description="Полная проверка восстановления в отдельной временной БД локального PostgreSQL"
    )
    parser.add_argument("dump", type=Path)
    parser.add_argument("--port", type=int, default=5432)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Запустите через sudo: требуется создание тестовой БД от postgres")
    if not args.dump.is_file() or args.dump.is_symlink():
        raise SystemExit("Укажите обычный файл резервной копии")
    name = "tgviewer_restore_test_" + secrets.token_hex(8)
    created = False
    try:
        run(
            ["createdb", "--port", str(args.port), "--template=template0", name],
            stdout=subprocess.DEVNULL,
        )
        created = True
        with args.dump.open("rb") as stream:
            run(
                [
                    "pg_restore",
                    "--port",
                    str(args.port),
                    "--dbname",
                    name,
                    "--no-owner",
                    "--no-privileges",
                    "--exit-on-error",
                ],
                stdin=stream,
                stdout=subprocess.DEVNULL,
            )
        run(
            [
                "psql",
                "--port",
                str(args.port),
                "--dbname",
                name,
                "--no-psqlrc",
                "--set=ON_ERROR_STOP=1",
                "--command",
                "SELECT count(*) AS accounts FROM telegram_accounts; SELECT count(*) AS jobs FROM jobs;",
            ],
            stdout=subprocess.PIPE,
        )
        print("[OK] Полное восстановление и чтение таблиц прошли успешно. Тестовая БД удаляется.")
    finally:
        if created:
            run(["dropdb", "--port", str(args.port), name], stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
