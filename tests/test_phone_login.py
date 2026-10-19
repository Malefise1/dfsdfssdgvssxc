import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import User
from telethon.tl.types.auth import CodeTypeSms, SentCode, SentCodeTypeApp

from app.errors import UserInputError
from app.services.imports import ImportItem
from app.services.onboarding import ActiveImport, OnboardingService


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.fixture
def login(settings, db, monkeypatch):
    user = User(id=99, first_name="Owner", phone="79991234567")
    sent = SentCode(SentCodeTypeApp(5), "hash-1", next_type=CodeTypeSms(), timeout=60)
    client = SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        send_code_request=AsyncMock(return_value=sent),
        sign_in=AsyncMock(return_value=user),
        is_user_authorized=AsyncMock(return_value=False),
        log_out=AsyncMock(),
        session=SimpleNamespace(save=lambda: "verified-session"),
    )
    monkeypatch.setattr("app.services.onboarding.build_user_client", lambda *a, **k: client)
    prompts = []

    async def send_message(*args, **kwargs):
        message = SimpleNamespace(
            message_id=len(prompts) + 1,
            delete=AsyncMock(),
            edit_reply_markup=AsyncMock(),
            chat=SimpleNamespace(id=1, type="private"),
        )
        prompts.append(message)
        return message

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send_message))

    @asynccontextmanager
    async def bootstrap(cancel):
        yield

    async def store(*args, **kwargs):
        client.disconnect.assert_awaited_once()
        return SimpleNamespace(display_number=1), True

    accounts = SimpleNamespace(store_verified=AsyncMock(side_effect=store), set_status=AsyncMock())
    runtime = SimpleNamespace(proxy_for=AsyncMock(return_value=None), bootstrap=bootstrap)
    service = OnboardingService(settings, db, accounts, runtime, bot)
    active = ActiveImport("batch-token", "batch", 1, login_method="code")
    service.active = active

    def begin(reauth_id=None):
        return asyncio.create_task(
            service._phone(active, ImportItem("one", "phone", "+79991234567"), 1, 1, reauth_id)
        )

    async def button(key, *, token=None, user=1, message=None):
        code = active.code
        await service.code_button(
            SimpleNamespace(
                from_user=SimpleNamespace(id=user),
                data=f"login:code:{token or code.token}:{key}",
                message=message or prompts[code.message_id - 1],
            )
        )

    async def enter(value="57294"):
        await until(lambda: active.code and active.code.message_id)
        for digit in value:
            await button(digit)
        await button("ok")

    return SimpleNamespace(
        service=service,
        active=active,
        client=client,
        sent=sent,
        user=user,
        bot=bot,
        prompts=prompts,
        accounts=accounts,
        begin=begin,
        button=button,
        enter=enter,
    )


async def test_code_login_stores_verified_session_and_clears_input(login):
    task = login.begin()
    await login.enter()
    account, added = await task
    assert added and account.display_number == 1
    login.client.sign_in.assert_awaited_once_with(
        phone="+79991234567", code="57294", phone_code_hash="hash-1"
    )
    login.accounts.store_verified.assert_awaited_once_with(
        "verified-session", login.user, 1, None, reauth_id=None
    )
    assert login.active.code is None
    login.prompts[0].delete.assert_awaited_once()
    assert "57294" not in str(login.bot.send_message.call_args_list)
    assert "57294" not in str(login.prompts[0].edit_reply_markup.call_args_list)


async def test_invalid_code_then_2fa_and_invalid_password_retry(login):
    login.client.sign_in.side_effect = [
        PhoneCodeInvalidError(None),
        SessionPasswordNeededError(None),
        PasswordHashInvalidError(None),
        login.user,
    ]
    task = login.begin()
    await login.enter("11111")
    await until(lambda: login.active.code and not login.active.code.result.done())
    await login.enter()
    for password in ("wrong-password", "correct-password"):
        await until(lambda: login.active.password and not login.active.password.done())
        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=1), text=password, delete=AsyncMock(), answer=AsyncMock()
        )
        assert await login.service.submit_password(msg)
        msg.delete.assert_awaited_once()
        # Wait for this input to be consumed before looking for the next prompt.
        future = login.active.password
        await until(lambda future=future: login.active.password is not future)
    assert (await task)[1]
    assert login.active.password is None and login.active.code is None
    assert "correct-password" not in str(login.bot.send_message.call_args_list)


@pytest.mark.parametrize("error", [PhoneCodeExpiredError(None), FloodWaitError(None, 30)])
async def test_expired_code_and_flood_wait_never_store_session(login, error):
    login.client.sign_in.side_effect = error
    task = login.begin()
    await login.enter()
    expected = UserInputError if isinstance(error, PhoneCodeExpiredError) else FloodWaitError
    with pytest.raises(expected):
        await task
    login.accounts.store_verified.assert_not_awaited()
    login.client.disconnect.assert_awaited_once()
    assert login.active.code is None


async def test_three_invalid_codes_stop(login):
    login.client.sign_in.side_effect = PhoneCodeInvalidError(None)
    task = login.begin()
    for _ in range(3):
        await until(lambda: login.active.code and not login.active.code.result.done())
        await login.enter("00000")
    with pytest.raises(UserInputError, match="Лимит попыток кода"):
        await task
    assert login.client.sign_in.await_count == 3
    login.accounts.store_verified.assert_not_awaited()


async def test_resend_waits_and_uses_new_hash(login):
    login.client.send_code_request.side_effect = [
        login.sent,
        SentCode(SentCodeTypeApp(5), "hash-2", next_type=CodeTypeSms(), timeout=60),
    ]
    task = login.begin()
    await until(lambda: login.active.code and login.active.code.message_id)
    old_token = login.active.code.token
    with pytest.raises(UserInputError, match="сек"):
        await login.button("resend")
    assert login.client.send_code_request.await_count == 1
    login.active.code.resend_at = time.monotonic() - 1
    await login.button("resend")
    await until(lambda: login.active.code and login.active.code.token != old_token)
    with pytest.raises(UserInputError, match="устарел"):
        await login.button("1", token=old_token)
    await login.enter()
    await task
    assert login.client.sign_in.call_args.kwargs["phone_code_hash"] == "hash-2"
    assert login.client.send_code_request.await_count == 2


@pytest.mark.parametrize("cancel_task", [False, True])
async def test_skip_and_shutdown_clear_code_and_disconnect(login, cancel_task):
    task = login.begin()
    await until(lambda: login.active.code and login.active.code.message_id)
    await login.button("9")
    code = login.active.code
    if cancel_task:
        task.cancel()
    else:
        login.service.cancel(1, skip=True)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert login.active.code is None and code.digits == ""
    login.client.disconnect.assert_awaited_once()
    login.accounts.store_verified.assert_not_awaited()
    login.prompts[0].delete.assert_awaited_once()


async def test_timeout_cleans_prompt(login):
    login.service.settings.login_ttl_seconds = 0.02
    task = login.begin()
    with pytest.raises(TimeoutError):
        await task
    assert login.active.code is None
    login.client.disconnect.assert_awaited_once()
    login.accounts.store_verified.assert_not_awaited()


async def test_keypad_rejects_other_actor_and_message_and_handles_edits(login):
    task = login.begin()
    await until(lambda: login.active.code and login.active.code.message_id)
    with pytest.raises(UserInputError):
        await login.button("9", user=2)
    with pytest.raises(UserInputError):
        await login.button(
            "9",
            message=SimpleNamespace(chat=SimpleNamespace(type="private", id=1), message_id=9999),
        )
    with pytest.raises(UserInputError, match="Введите 5"):
        await login.button("ok")
    for key in ("9", "8", "back", "clear"):
        await login.button(key)
    assert login.active.code.digits == ""
    await login.enter()
    await task


async def test_chat_code_deleted_and_not_passed_to_telegram(login):
    task = login.begin()
    await until(lambda: login.active.code and login.active.code.message_id)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1), text="12345", delete=AsyncMock(), answer=AsyncMock()
    )
    assert await login.service.reject_code_message(message)
    message.delete.assert_awaited_once()
    login.client.sign_in.assert_not_awaited()
    assert login.active.code.digits == ""
    await login.enter()
    await task


async def test_wrong_account_not_stored_and_new_authorization_revoked(login):
    login.client.sign_in.return_value = User(id=100, phone="79990000000")
    login.client.is_user_authorized.return_value = True
    task = login.begin()
    await login.enter()
    with pytest.raises(UserInputError, match="другой номер"):
        await task
    login.accounts.store_verified.assert_not_awaited()
    login.client.log_out.assert_awaited_once()


async def test_reauth_must_match_existing_telegram_id(login, seeded):
    # Same phone but a different Telegram ID must never replace the stored account.
    task = login.begin(reauth_id=seeded.accounts[0].id)
    await login.enter()
    with pytest.raises(UserInputError, match="другой аккаунт"):
        await task
    login.accounts.set_status.assert_awaited_once()
    login.accounts.store_verified.assert_not_awaited()
