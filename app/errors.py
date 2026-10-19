from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)


class UserInputError(ValueError):
    pass


class AccountUnavailable(RuntimeError):
    pass


class SessionInvalid(AccountUnavailable):
    pass


AUTH_ERRORS = (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    SessionInvalid,
)


class ProxyUnavailable(RuntimeError):
    pass


class PausedError(ValueError):
    pass


def error_kind(exc):
    if isinstance(exc, AUTH_ERRORS):
        return "authorization"
    if isinstance(exc, ProxyUnavailable):
        return "proxy"
    if isinstance(exc, (ConnectionError, OSError, TimeoutError)):
        return "network"
    return "operation"


def safe_error(exc):
    # RPC exception text may contain phone numbers, proxy credentials or user data.
    names = {
        "FloodWaitError": "Telegram назначил ожидание",
        "SessionPasswordNeededError": "Нужен пароль 2FA",
        "PasswordHashInvalidError": "Неверный пароль 2FA",
        "PhoneCodeInvalidError": "Неверный код входа",
        "PhoneCodeEmptyError": "Введите код входа",
        "PhoneCodeExpiredError": "Код истёк или аннулирован; начните вход заново",
        "PhoneNumberInvalidError": "Некорректный номер телефона",
        "PhoneNumberBannedError": "Telegram заблокировал этот номер",
        "PhoneNumberUnoccupiedError": "Аккаунт с этим номером не зарегистрирован в Telegram",
        "PhoneNumberAppSignupForbiddenError": "Для этого номера нужен вход через официальное приложение",
        "SendCodeUnavailableError": "Telegram сейчас не может отправить код; используйте QR или повторите позже",
        "ApiIdInvalidError": "Проверьте API_ID и API_HASH в настройках сервера",
        "ChannelPrivateError": "Канал недоступен",
        "UsernameNotOccupiedError": "Канал не найден",
        "AuthKeyDuplicatedError": "Сессия отозвана: использовалась одновременно",
        "AuthKeyUnregisteredError": "Нужна повторная авторизация",
        "SessionRevokedError": "Сессия отозвана",
        "TimeoutError": "Превышено время ожидания",
        "InvalidToken": "Повреждённая сессия или другой ключ шифрования",
    }
    if isinstance(exc, (AccountUnavailable, ProxyUnavailable, PausedError, UserInputError)):
        return str(exc)
    return names.get(
        type(exc).__name__, f"Ошибка {type(exc).__name__}; подробности доступны администратору"
    )
