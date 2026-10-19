import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.enums import AccountStatus, ProxyProtocol
from app.errors import ProxyUnavailable
from app.models import Proxy, TelegramAccount, utcnow
from app.services.accounts import AccountService
from app.services.control import put_setting
from app.services.runtime import AccountRuntime, ManagedClient


class Client:
    current = 0
    peak = 0

    def __init__(self, *args, **kwargs):
        pass

    async def connect(self):
        Client.current += 1
        Client.peak = max(Client.peak, Client.current)

    async def is_user_authorized(self):
        return True

    async def disconnect(self):
        Client.current -= 1


@pytest.fixture
def runtime(settings, db, seeded, monkeypatch):
    monkeypatch.setattr("app.services.runtime.build_user_client", Client)
    Client.current = Client.peak = 0
    return AccountRuntime(settings, db, seeded.box)


async def test_same_account_serialized_even_across_purposes(runtime, seeded):
    async def use(purpose):
        async with runtime.connection(seeded.accounts[0].id, purpose):
            await asyncio.sleep(0.01)

    await asyncio.gather(use("job"), use("subscription"), use("health"))
    assert Client.peak == 1 and Client.current == 0


async def test_cancel_at_acquisition_does_not_leak_lock_or_slot(runtime, seeded):
    stopped = asyncio.Event()
    stopped.set()
    with pytest.raises(asyncio.CancelledError):
        async with runtime.connection(seeded.accounts[0].id, "race", stopped):
            pass
    async with asyncio.timeout(1):
        async with runtime.connection(seeded.accounts[0].id, "after cancel"):
            pass
    assert runtime._slots._value == runtime.settings.max_concurrent_accounts
    assert not runtime._locks[seeded.accounts[0].id].locked()


async def test_disabled_account_interrupts_running_and_waiting(runtime, seeded, settings, db):
    started = asyncio.Event()

    async def use():
        async with runtime.connection(seeded.accounts[0].id, "long"):
            started.set()
            await asyncio.Event().wait()

    a = asyncio.create_task(use())
    await started.wait()
    b = asyncio.create_task(use())
    await asyncio.sleep(0.01)
    service = AccountService(settings, db, seeded.box, runtime)
    await service.set_status(seeded.accounts[0].id, AccountStatus.DISABLED, 1)
    assert a.cancelled() and b.cancelled() and Client.current == 0


async def test_required_proxy_never_falls_back(runtime, db, seeded):
    await put_setting(db, "proxy_policy", {"value": "required"})
    with pytest.raises(ProxyUnavailable):
        await runtime.proxy_for(seeded.accounts[0].id)


async def test_proxy_assignment_stable_and_failure_closed(runtime, db, seeded):
    async with db.session() as session:
        proxies = [
            Proxy(
                protocol=ProxyProtocol.SOCKS5,
                host="proxy" + str(i) + ".example",
                port=1080,
                login="u",
                encrypted_password=seeded.box.encrypt("secret"),
                active=True,
                last_checked_at=utcnow(),
                last_check_ok=True,
            )
            for i in range(2)
        ]
        session.add_all(proxies)
    first = await runtime.proxy_for(seeded.accounts[0].id)
    assert (await runtime.proxy_for(seeded.accounts[0].id)).id == first.id
    second = await runtime.proxy_for(seeded.accounts[1].id)
    assert second.id != first.id
    async with db.session() as session:
        (await session.get(Proxy, first.id)).last_check_ok = False
    with pytest.raises(ProxyUnavailable):
        await runtime.proxy_for(seeded.accounts[0].id)


async def test_flood_wait_saved_and_cancelled_without_proxy_rotation(runtime, db, seeded):
    from telethon.errors import FloodWaitError

    client = SimpleNamespace()
    cancel = asyncio.Event()
    managed = ManagedClient(runtime, seeded.accounts[0], client, cancel)
    factory = AsyncMock(side_effect=FloodWaitError(None, 3600))
    task = asyncio.create_task(managed.request(factory))
    for _ in range(50):
        async with db.session() as session:
            row = await session.get(TelegramAccount, seeded.accounts[0].id)
        if row.flood_until:
            break
        await asyncio.sleep(0.01)
    assert row.flood_until
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert factory.call_count == 1
