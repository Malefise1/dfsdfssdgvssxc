#!/usr/bin/env python3
"""Explicit, isolated installation/update. Never installs/upgrades OS packages or restarts PostgreSQL."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import secrets
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command, **kwargs):
    return subprocess.run([str(x) for x in command], check=True, **kwargs)


def psql(sql, port):
    return run(
        [
            "runuser",
            "-u",
            "postgres",
            "--",
            "psql",
            "--port",
            port,
            "--no-psqlrc",
            "--set=ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "postgres",
        ],
        input=sql,
        text=True,
        capture_output=True,
    ).stdout.strip()


def write(path, text, mode=0o644):
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def build(source, target):
    if target.exists():
        raise ValueError("Каталог релиза уже существует")
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("Исходный пакет не должен содержать символические ссылки")
    # Never copy credentials, mutable data, or a developer virtual environment.
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns(
            ".env",
            ".env.*.local",
            ".venv",
            "venv",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            ".git",
            "data",
            "logs",
            "reports",
            "backups",
            "*.pyc",
            "*.session",
            "*.dump",
        ),
    )
    for path in target.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    run(["python3.12", "-m", "venv", target / ".venv"])
    run(
        [
            target / ".venv/bin/python",
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--require-hashes",
            "-r",
            target / "requirements.txt",
        ]
    )
    run([target / ".venv/bin/python", "-c", "import app.main"], cwd=target)


def service_env(instance):
    # Do not inherit arbitrary Python module paths or DB variables from the root shell.
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "TGVIEWER_ENV_FILE": f"/etc/{instance}/app.env",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
    }


def app(instance, release, *args, **kwargs):
    return run(
        ["runuser", "-u", instance, "--", release / ".venv/bin/python", *args],
        cwd=release,
        env=service_env(instance),
        **kwargs,
    )


def switch(root, release):
    tmp = root / "current.next"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(release)
    os.replace(tmp, root / "current")


def validate_root(instance):
    root = Path("/opt") / instance
    marker = root / "instance.json"
    if not marker.is_file():
        raise ValueError("Нет маркера установленного экземпляра")
    info = json.loads(marker.read_text())
    if info.get("instance") != instance or info.get("format") != 2:
        raise ValueError("Другой формат установки")
    return root, info


def install(args):
    name = args.instance
    root = Path("/opt") / name
    paths = [
        root,
        Path("/etc") / name,
        Path("/var/lib") / name,
        Path("/var/log") / name,
        Path("/etc/systemd/system") / (name + ".service"),
    ]
    if any(path.exists() for path in paths):
        raise ValueError("Имя уже занято каталогом или службой; выберите другое --instance")
    try:
        pwd.getpwnam(name)
    except KeyError:
        pass
    else:
        raise ValueError("Такой системный пользователь уже существует")
    if psql(
        f"SELECT 1 FROM pg_roles WHERE rolname='{name}'; SELECT 1 FROM pg_database WHERE datname='{name}';",
        args.pg_port,
    ):
        raise ValueError("Такое имя роли/базы PostgreSQL уже занято")
    for command in ("python3.12", "pg_dump", "pg_restore", "systemctl", "useradd"):
        if not shutil.which(command):
            raise ValueError("Не установлен " + command + "; см. DEPLOYMENT.md")
    # All names are validated; credentials are generated locally and passed via stdin, never argv.
    root.mkdir(mode=0o755)
    release = root / "releases" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    build(ROOT, release)
    run(
        [
            "useradd",
            "--system",
            "--user-group",
            "--home-dir",
            f"/var/lib/{name}",
            "--no-create-home",
            "--shell",
            "/usr/sbin/nologin",
            name,
        ]
    )
    account = pwd.getpwnam(name)
    config = Path("/etc") / name
    config.mkdir(mode=0o750)
    os.chown(config, 0, account.pw_gid)
    data = Path("/var/lib") / name
    logs = Path("/var/log") / name
    for path in (data, logs, data / "reports", data / "backups"):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(path, account.pw_uid, account.pw_gid)
    password = secrets.token_hex(24)
    psql(
        f"CREATE ROLE {name} LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 15; CREATE DATABASE {name} OWNER {name}; REVOKE CONNECT ON DATABASE {name} FROM PUBLIC; GRANT CONNECT ON DATABASE {name} TO {name};",
        args.pg_port,
    )
    # Base64 Fernet key from OS randomness; no dependency import in root's Python environment.
    import base64

    values = {
        "BOT_TOKEN": "",
        "MAIN_ADMIN_ID": "",
        "API_ID": "",
        "API_HASH": "",
        "DATABASE_URL": f"postgresql+asyncpg://{name}:{password}@127.0.0.1:{args.pg_port}/{name}",
        "SESSION_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "TIMEZONE": "Europe/Moscow",
        "PROXY_POLICY": "required",
        "MAX_CONCURRENT_ACCOUNTS": "5",
        "DATA_DIR": str(data),
        "REPORTS_DIR": str(data / "reports"),
        "BACKUPS_DIR": str(data / "backups"),
        "LOGS_DIR": str(logs),
    }
    envfile = config / "app.env"
    write(
        envfile,
        "".join(key + "=" + json.dumps(value) + "\n" for key, value in values.items()),
        0o640,
    )
    os.chown(envfile, 0, account.pw_gid)
    switch(root, release)
    write(
        root / "instance.json",
        json.dumps({"format": 2, "instance": name, "pg_port": args.pg_port}, indent=2),
    )
    unit = (
        (ROOT / "deploy/systemd/telegram-channel-viewer.service")
        .read_text()
        .replace("@INSTANCE@", name)
    )
    write(Path("/etc/systemd/system") / (name + ".service"), unit)
    run(["systemctl", "daemon-reload"])  # reloads definitions; never restarts any existing service
    print(f"Готов отдельный экземпляр {name}. Служба пока НЕ запущена.")
    print(f"Настройка: sudo {release}/.venv/bin/python {release}/manage.py configure {envfile}")
    print(f"Затем: sudo python3 {ROOT}/scripts/deploy.py activate --instance {name}")


def activate(args):
    root, _ = validate_root(args.instance)
    release = (root / "current").resolve()
    app(args.instance, release, "manage.py", "migrate")
    app(args.instance, release, "doctor.py")
    run(["systemctl", "enable", "--now", args.instance + ".service"])
    print("Служба запущена. В боте настройте прокси/аккаунты и снимите общую паузу.")


def update(args):
    name = args.instance
    root, _ = validate_root(name)
    previous = (root / "current").resolve()
    release = root / "releases" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    build(ROOT, release)  # dependency failures leave the current service running
    unit = name + ".service"
    run(["systemctl", "stop", unit])
    # Back up using new code: the old backup implementation did not guarantee atomic completion.
    try:
        result = app(name, release, "manage.py", "backup", capture_output=True, text=True)
        dump = result.stdout.strip().splitlines()[-1]
        snapshot = root / ("before-update-" + release.name + ".json")
        write(
            snapshot,
            json.dumps({"previous": str(previous), "next": str(release), "dump": dump}, indent=2),
            0o600,
        )
        app(name, release, "manage.py", "migrate")
        app(name, release, "doctor.py")
        switch(root, release)
        run(["systemctl", "start", unit])
        run(["systemctl", "is-active", "--quiet", unit])
    except Exception:
        print(
            "Обновление остановлено. Только эта служба оставлена выключенной. Данные не удалены. См. UPGRADE.md и before-update-*.json.",
            file=sys.stderr,
        )
        raise
    print(f"Обновление готово. Предыдущий релиз: {previous.name}. Резервная копия: {dump}")


def rollback(args):
    name = args.instance
    root, _ = validate_root(name)
    target = (root / "releases" / args.release).resolve()
    if target.parent != (root / "releases").resolve() or not (target / "app").is_dir():
        raise ValueError("Нет такого локального релиза")
    # A code rollback is allowed only if the old code accepts the current schema.
    app(name, target, "doctor.py")
    run(["systemctl", "stop", name + ".service"])
    switch(root, target)
    run(["systemctl", "start", name + ".service"])
    print("Код переключён на проверенный релиз. База не изменялась.")


def main():
    parser = argparse.ArgumentParser(
        description="Установка отдельного экземпляра без изменения других проектов"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("install", "activate", "update", "rollback"):
        part = sub.add_parser(command)
        part.add_argument("--instance", default="tgviewer2")
        if command == "install":
            part.add_argument("--pg-port", type=int, default=5432)
        if command == "rollback":
            part.add_argument("release")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Запустите через sudo")
    if not re.fullmatch(r"tgviewer[a-z0-9_]{0,15}", args.instance):
        raise SystemExit(
            "Имя должно начинаться с tgviewer; далее строчные латинские буквы, цифры, _. Максимум 23 символа."
        )
    if sys.version_info < (3, 12):  # noqa: UP036 - may run before dependencies are installed
        raise SystemExit("Для установщика требуется Python 3.12+")
    try:
        globals()[args.command](args)
    except Exception as exc:
        if type(exc) is ValueError:
            print(str(exc), file=sys.stderr)
        print(
            f"Операция прервана: {type(exc).__name__}. Проверьте предыдущие сообщения. Другие службы не изменялись.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
