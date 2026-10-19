from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import secrets
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from sqlalchemy import select
from telethon.client.telegrambaseclient import DEFAULT_DC_ID, DEFAULT_IPV4_IP
from telethon.network import ConnectionTcpFull
from telethon.network.mtprotoplainsender import MTProtoPlainSender
from telethon.tl.functions import ReqPqMultiRequest
from telethon.tl.types import ResPQ

from app.enums import ProxyProtocol
from app.models import Proxy, utcnow
from app.telegram.client import ProxyConfig


@dataclass(frozen=True, slots=True)
class ParsedProxy:
    login: str
    password: str = field(repr=False)
    host: str
    port: int
    protocol: ProxyProtocol | None = None


def normalize_host(value):
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        pass
    value = value.rstrip(".").lower()
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("Некорректный адрес прокси") from None
    if (
        len(value) > 253
        or re.fullmatch(r"[0-9.]+", value)
        or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x) for x in value.split(".")
        )
    ):
        raise ValueError("Некорректный адрес прокси")
    return value


def parse_proxy_line(line):
    value = line.strip().lstrip("\ufeff")
    if (
        not value
        or len(value) > 2048
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("Пустая строка, пробелы или слишком длинные реквизиты")
    protocol = None
    if "://" in value:
        scheme, value = value.split("://", 1)
        try:
            protocol = ProxyProtocol(scheme.lower())
        except ValueError:
            raise ValueError("Поддерживаются SOCKS5 и HTTP (не HTTPS/SOCKS4/MTProxy)") from None
    login = password = ""
    if not value.startswith("[") and re.match(r"^[^:@]+:\d+:[^:]+:", value):
        host, port, login, password = value.split(":", 3)
        endpoint = f"{host}:{port}"
    elif "@" in value:
        auth, endpoint = value.rsplit("@", 1)
        if ":" not in auth:
            raise ValueError("Укажите логин и пароль через двоеточие")
        login, password = auth.split(":", 1)
    else:
        endpoint = value
        # Provider export host:port:login:password; IPv6 must use brackets.
        if not value.startswith("[") and value.count(":") >= 3:
            host, port, login, password = value.split(":", 3)
            endpoint = f"{host}:{port}"
    try:
        parsed = urlsplit("//" + endpoint)
        if parsed.path or parsed.query or parsed.fragment or "?" in endpoint or "#" in endpoint:
            raise ValueError
        host, port = parsed.hostname, parsed.port
        if not host or not port or not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError("Ожидается host:port или login:password@host:port; порт 1–65535") from None
    if protocol is not None:
        if re.search(r"%(?![0-9a-fA-F]{2})", login + password):
            raise ValueError("Некорректное %-кодирование логина или пароля")
        try:
            login, password = unquote(login, errors="strict"), unquote(password, errors="strict")
        except UnicodeError:
            raise ValueError("Некорректное кодирование реквизитов") from None
    if bool(login) != bool(password):
        raise ValueError("Нужны и логин, и пароль; либо прокси без авторизации")
    if any(ord(c) < 32 or ord(c) == 127 for c in login + password):
        raise ValueError("Управляющие символы в реквизитах запрещены")
    if len(login.encode()) > 255 or len(password.encode()) > 1024:
        raise ValueError("Слишком длинные реквизиты")
    return ParsedProxy(login, password, normalize_host(host), port, protocol)


def parse_proxy_file(text):
    rows, errors, seen = [], [], {}
    for number, line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if len(rows) + len(errors) >= 5000:
            raise ValueError("Не более 5000 прокси в одном списке")
        try:
            row = parse_proxy_line(line)
            key = row.protocol, row.host, row.port, row.login
            if key in seen:
                if seen[key] != row.password:
                    raise ValueError("Один прокси указан с разными паролями")
                continue
            seen[key] = row.password
            rows.append(row)
        except ValueError as exc:
            errors.append((number, str(exc)))
    return rows, errors


def proxy_error(exc):
    # Provider exceptions can contain credentials. Never forward their text.
    if isinstance(exc, TimeoutError):
        return "Нет ответа за 12 с. Проверьте адрес, порт и доступ к Telegram."
    if type(exc).__name__ in {"ProxyError", "ProxyConnectionError"}:
        return "Прокси отказал: проверьте протокол, логин/пароль и разрешённый IP сервера."
    if isinstance(exc, (OSError, ConnectionError)):
        return "Соединение закрыто или адрес недоступен. Проверьте порт и тип прокси."
    return "Проверка Telegram не пройдена. Проверьте реквизиты и доступ к MTProto."


async def probe_mtproto(config):
    """A plain req_pq exchange: no API credentials, auth key or account session."""
    # Isolated logger: proxy library exceptions must not disclose credentials.
    log = logging.Logger("proxy.probe")
    log.addHandler(logging.NullHandler())
    loggers = defaultdict(lambda: log)
    connection = ConnectionTcpFull(
        DEFAULT_IPV4_IP,
        443,
        DEFAULT_DC_ID,
        loggers=loggers,
        proxy=config.telethon_value(),
    )
    try:
        async with asyncio.timeout(12):
            await connection.connect(timeout=10)
            nonce = int.from_bytes(secrets.token_bytes(16), "little", signed=True)
            result = await MTProtoPlainSender(connection, loggers=loggers).send(
                ReqPqMultiRequest(nonce)
            )
            if not isinstance(result, ResPQ) or result.nonce != nonce:
                raise ConnectionError("Invalid MTProto response")
    finally:
        with suppress(Exception):
            await asyncio.wait_for(connection.disconnect(), 3)


class ProxyService:
    def __init__(self, db, secrets):
        self.db, self.secrets = db, secrets
        self._lock = asyncio.Lock()
        self._check_lock = asyncio.Lock()

    async def delete_many(self, proxy_ids, runtime, actor_id):
        # Imports cannot replace a selected endpoint while deletion is in progress.
        async with self._lock:
            return await runtime.delete_proxies(proxy_ids, actor_id)

    async def import_many(self, protocol, rows):
        added, seen = 0, {}
        # Validate the whole batch before starting the transaction.
        for row in rows:
            kind = row.protocol or ProxyProtocol(protocol)
            if kind == ProxyProtocol.SOCKS5 and len(row.password.encode()) > 255:
                raise ValueError("Пароль SOCKS5 не должен превышать 255 байт")
            key = kind, row.host, row.port, row.login
            if key in seen and seen[key] != row.password:
                raise ValueError("Один прокси указан с разными паролями; ничего не сохранено")
            seen[key] = row.password
        async with self._lock, self.db.session() as session:
            for (kind, host, port, login), password in seen.items():
                existing = await session.scalar(
                    select(Proxy).where(
                        Proxy.protocol == kind,
                        Proxy.host == host,
                        Proxy.port == port,
                        Proxy.login == login,
                    )
                )
                if existing is None:
                    existing = Proxy(protocol=kind, host=host, port=port, login=login)
                    session.add(existing)
                    existing.encrypted_password = self.secrets.encrypt(password)
                    added += 1
                elif self.secrets.decrypt(existing.encrypted_password) != password:
                    existing.encrypted_password = self.secrets.encrypt(password)
                    existing.last_check_ok = existing.last_checked_at = existing.last_error = None
                # Re-import never re-enables a disabled endpoint or resets a valid check.
                await session.flush()
        return added

    async def check_proxy(self, proxy):
        ok, message = False, None
        try:
            config = ProxyConfig(
                proxy.id,
                proxy.protocol,
                proxy.host,
                proxy.port,
                proxy.login,
                self.secrets.decrypt(proxy.encrypted_password),
            )
            await probe_mtproto(config)
            ok = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = proxy_error(exc)
        async with self.db.session() as session:
            row = await session.get(Proxy, proxy.id)
            # A check cannot approve credentials changed during the network request.
            if row and row.active and row.encrypted_password == proxy.encrypted_password:
                row.last_check_ok, row.last_checked_at, row.last_error = ok, utcnow(), message
            else:
                ok = False
        return ok

    async def check_all(self):
        if self._check_lock.locked():
            raise ValueError("Проверка прокси уже идёт")
        async with self._check_lock:
            async with self.db.session() as session:
                proxies = list(
                    (await session.scalars(select(Proxy).where(Proxy.active.is_(True)))).all()
                )
            slots = asyncio.Semaphore(5)

            async def check(proxy):
                async with slots:
                    return await self.check_proxy(proxy)

            results = await asyncio.gather(*(check(proxy) for proxy in proxies))
            return sum(results), len(proxies)
