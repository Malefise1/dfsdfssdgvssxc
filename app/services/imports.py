"""Bounded import of Telethon sessions. Never execute archive contents or SQL scripts."""

from __future__ import annotations

import io
import ipaddress
import json
import sqlite3
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from app.services.accounts import fingerprint, normalize_phone


@dataclass
class ImportItem:
    name: str
    kind: str
    value: str = field(repr=False)
    note: str = ""


def session_from_sqlite(data):
    if not data.startswith(b"SQLite format 3\x00") or len(data) > 2_000_000:
        raise ValueError("Некорректный или слишком большой файл .session")
    with tempfile.TemporaryDirectory(prefix="tgviewer-import-") as directory:
        path = Path(directory) / "input.session"
        path.write_bytes(data)
        path.chmod(0o600)
        connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.set_progress_handler(lambda: 1, 50000)
            row = connection.execute(
                "SELECT dc_id, server_address, port, auth_key FROM sessions LIMIT 1"
            ).fetchone()
            if not row or not row[3] or len(row[3]) != 256:
                raise ValueError("Файл не содержит авторизованную сессию Telethon")
            dc, address, port, key = row
            ipaddress.ip_address(address)
            if not 1 <= dc <= 100 or not 1 <= port <= 65535:
                raise ValueError("Некорректный адрес дата-центра")
            session = StringSession()
            session.set_dc(dc, address, port)
            session.auth_key = AuthKey(key)
            return session.save()
        finally:
            connection.close()


def phone_from_metadata(value):
    """Normalize phone values used by session+JSON export tools.

    Some exporters store the international number as digits without the leading
    plus. This is accepted only for an explicit JSON ``phone`` field; free-form
    TXT input keeps requiring the unambiguous ``+...`` form.
    """

    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        raise ValueError("Поле phone в JSON должно содержать номер")
    value = value.strip()
    if value.isdigit():
        value = "+" + value
    return normalize_phone(value)


def json_rows(name, content):
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{name}: JSON должен быть в UTF-8") from exc
    try:
        rows = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name}: некорректный JSON") from exc
    if isinstance(rows, dict):
        rows = rows.get("accounts", [rows])
    if not isinstance(rows, list):
        raise ValueError("JSON должен содержать список accounts")
    return rows


def parse_upload(filename, data, settings):
    if len(data) > settings.max_upload_bytes:
        raise ValueError("Файл превышает допустимый размер")
    items = []

    def append(item):
        if len(items) >= settings.max_import_accounts:
            raise ValueError("Превышен лимит записей в импорте")
        items.append(item)

    def parse(name, content):
        suffix = Path(name).suffix.lower()
        if suffix == ".session":
            if not content:
                raise ValueError(
                    f"{name}: пустая .session не содержит ключ авторизации; нужен JSON с номером для входа через QR"
                )
            append(ImportItem(name, "session", session_from_sqlite(content)))
            return
        try:
            decoded = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Текстовый файл должен быть в UTF-8") from exc
        if suffix == ".json":
            rows = json_rows(name, content)
            for index, row in enumerate(rows, 1):
                if not isinstance(row, dict):
                    raise ValueError("Элемент JSON должен быть объектом")
                value = row.get("session") or row.get("string_session")
                if isinstance(value, str) and value.strip():
                    add(f"{name}:{index}", value.strip())
                elif "phone" in row:
                    append(
                        ImportItem(
                            f"{name}:{index}", "phone", phone_from_metadata(row.get("phone"))
                        )
                    )
                else:
                    raise ValueError("В JSON нужен session, string_session или phone")
        elif suffix == ".txt":
            for index, line in enumerate(decoded.splitlines(), 1):
                if line.strip():
                    add(f"{name}:{index}", line.strip())
        else:
            raise ValueError("Поддерживаются .session, .txt, .json и .zip")

    def add(name, value):
        if value.startswith("+"):
            append(ImportItem(name, "phone", normalize_phone(value)))
        else:
            fingerprint(value)
            append(ImportItem(name, "session", value))

    if filename.lower().endswith(".zip"):
        try:
            with ZipFile(io.BytesIO(data)) as archive:
                entries = [entry for entry in archive.infolist() if not entry.is_dir()]
                # Pair-based exporters use one .session plus one JSON sidecar per
                # account. The account limit is enforced by append(); this is a
                # separate, bounded archive-entry limit.
                if not entries or len(entries) > settings.max_import_accounts * 3:
                    raise ValueError("Недопустимое количество файлов в ZIP")
                if sum(entry.file_size for entry in entries) > settings.max_import_expanded_bytes:
                    raise ValueError("Распакованный ZIP слишком большой")
                files = {}
                for entry in entries:
                    path = PurePosixPath(entry.filename.replace("\\", "/"))
                    if (
                        path.is_absolute()
                        or ".." in path.parts
                        or ":" in entry.filename
                        or stat.S_ISLNK(entry.external_attr >> 16)
                    ):
                        raise ValueError("Недопустимый путь в архиве")
                    if (
                        entry.flag_bits & 1
                        or entry.file_size > 2_000_000
                        or entry.file_size > max(1, entry.compress_size) * 200
                    ):
                        raise ValueError("Недопустимое сжатие или размер файла в ZIP")
                    if path.suffix.lower() not in {".session", ".txt", ".json"}:
                        raise ValueError("В ZIP допустимы только .session, .txt и .json")
                    key = str(path).casefold()
                    if key in files:
                        raise ValueError("В ZIP повторяется имя файла")
                    files[key] = (path, archive.read(entry))

                consumed = set()
                # tpanel.online and compatible exporters create a JSON sidecar
                # next to every SQLite .session. Treat the pair as one account:
                # a real SQLite session is imported directly; a zero-byte
                # placeholder falls back to the phone from JSON and therefore
                # goes through the normal QR confirmation flow.
                for key, (path, content) in files.items():
                    if path.suffix.lower() != ".json":
                        continue
                    rows = json_rows(path.name, content)
                    if (
                        len(rows) != 1
                        or not isinstance(rows[0], dict)
                        or "session_file" not in rows[0]
                        or "phone" not in rows[0]
                    ):
                        continue
                    row = rows[0]
                    reference = row.get("session_file")
                    if not isinstance(reference, (str, int)):
                        raise ValueError(f"{path.name}: некорректное поле session_file")
                    reference = str(reference).strip().replace("\\", "/")
                    reference_path = PurePosixPath(reference)
                    if reference_path.is_absolute() or ".." in reference_path.parts:
                        raise ValueError(f"{path.name}: недопустимое имя session_file")
                    reference_name = reference_path.name
                    if not reference_name.lower().endswith(".session"):
                        reference_name += ".session"
                    candidates = [
                        str(path.with_name(reference_name)).casefold(),
                        str(path.with_suffix(".session")).casefold(),
                    ]
                    session_key = next(
                        (candidate for candidate in candidates if candidate in files), None
                    )
                    consumed.add(key)
                    if session_key is None:
                        append(
                            ImportItem(
                                path.name,
                                "phone",
                                phone_from_metadata(row.get("phone")),
                                "missing_session",
                            )
                        )
                        continue
                    consumed.add(session_key)
                    session_path, session_content = files[session_key]
                    if session_content:
                        append(
                            ImportItem(
                                session_path.name,
                                "session",
                                session_from_sqlite(session_content),
                            )
                        )
                    else:
                        append(
                            ImportItem(
                                path.name,
                                "phone",
                                phone_from_metadata(row.get("phone")),
                                "empty_session",
                            )
                        )

                for key, (path, content) in files.items():
                    if key not in consumed:
                        parse(path.name, content)
        except (BadZipFile, sqlite3.DatabaseError) as exc:
            raise ValueError("Повреждённый архив или файл сессии") from exc
    else:
        parse(Path(filename).name, data)
    if not 1 <= len(items) <= settings.max_import_accounts:
        raise ValueError(f"В одном импорте допускается 1-{settings.max_import_accounts} аккаунтов")
    unique, seen = [], set()
    for item in items:
        key = (item.kind, fingerprint(item.value) if item.kind == "session" else item.value)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique
