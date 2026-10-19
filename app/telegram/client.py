from __future__ import annotations

import platform
from dataclasses import dataclass, field

import socks
from telethon import TelegramClient
from telethon.sessions import StringSession

from app.enums import ProxyProtocol


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    id: int
    protocol: ProxyProtocol
    host: str
    port: int
    login: str
    password: str = field(repr=False)

    def telethon_value(self):
        kind = socks.SOCKS5 if self.protocol == ProxyProtocol.SOCKS5 else socks.HTTP
        return kind, self.host, self.port, True, self.login or None, self.password or None


def build_user_client(settings, session_string, proxy, account_number=0, *, receive_updates=False):
    return TelegramClient(
        StringSession(session_string),
        settings.api_id,
        settings.api_hash,
        proxy=proxy.telethon_value() if proxy else None,
        device_model=f"Channel Viewer #{account_number}",
        system_version=platform.system(),
        app_version="2.0.0",
        lang_code="ru",
        system_lang_code="ru-RU",
        auto_reconnect=True,
        connection_retries=3,
        request_retries=3,
        retry_delay=2,
        flood_sleep_threshold=0,
        receive_updates=receive_updates,
        raise_last_call_error=True,
        timeout=settings.rpc_timeout_seconds,
    )
