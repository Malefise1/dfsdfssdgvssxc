from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


class InvalidChannelLink(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PublicChannelLink:
    username: str

    @property
    def url(self) -> str:
        return f"https://t.me/{self.username}"


def parse_public_channel_link(raw: str) -> PublicChannelLink:
    value = raw.strip().rstrip("/")
    if not value:
        raise InvalidChannelLink("Пустая ссылка")

    if value.startswith("@"):
        username = value[1:]
    elif "://" not in value and "/" not in value:
        username = value
    else:
        if not value.startswith(("http://", "https://")):
            value = "https://" + value
        parsed = urlparse(value)
        if parsed.hostname not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
            raise InvalidChannelLink("Разрешены только ссылки t.me на публичные каналы")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise InvalidChannelLink("В ссылке отсутствует имя канала")
        if parts[0] in {"joinchat", "c", "s"} or parts[0].startswith("+"):
            raise InvalidChannelLink("Закрытые каналы и ссылки-приглашения не поддерживаются")
        username = parts[0]

    if not USERNAME_RE.fullmatch(username):
        raise InvalidChannelLink("Некорректное имя публичного канала")
    return PublicChannelLink(username=username)


def parse_channel_links(text: str) -> list[PublicChannelLink]:
    tokens = re.split(r"[\s,;]+", text.strip())
    links: list[PublicChannelLink] = []
    seen: set[str] = set()
    for token in tokens:
        if not token:
            continue
        link = parse_public_channel_link(token)
        key = link.username.lower()
        if key not in seen:
            links.append(link)
            seen.add(key)
    if not links:
        raise InvalidChannelLink("Не найдено ни одной ссылки")
    return links
