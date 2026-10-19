from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    """Encrypts Telegram sessions and proxy credentials at application level."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode("ascii"))

    def encrypt(self, value: str) -> str:
        if not value:
            return ""
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError(
                "Не удалось расшифровать секрет: неверный ключ или повреждённые данные"
            ) from exc
