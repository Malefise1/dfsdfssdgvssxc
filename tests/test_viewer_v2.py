import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from telethon.tl.types import Channel, Message, PeerChannel

from app.telegram.viewer import PublicChannelUnavailable, view_public_channel


class Client:
    def __init__(self):
        self.requests = []
        self.entity = Channel(id=10, title="Title", photo=None, date=datetime.now(), broadcast=True)

    async def get_entity(self, username):
        return self.entity

    async def __call__(self, request):
        self.requests.append(request)

    def iter_messages(self, entity, **kwargs):
        async def generate():
            for i in range(1, min(kwargs.get("limit") or 150, 150) + 1):
                yield Message(id=i, peer_id=PeerChannel(10), message="Post")

        return generate()


async def test_round_robin_batch_limit_and_durable_checkpoint():
    client = Client()
    progress = AsyncMock()
    resolve = AsyncMock()
    result = await view_public_channel(
        client,
        "channel",
        cancel_event=asyncio.Event(),
        on_progress=progress,
        on_resolve=resolve,
        max_messages=100,
    )
    assert result.viewed_posts == 100 and not result.scan_complete
    assert len(client.requests) == 1 and len(client.requests[0].id) == 100
    progress.assert_awaited_once_with(100, 100)
    resolve.assert_awaited_once_with(10)


async def test_changed_channel_identity_fails_before_view_request():
    client = Client()
    with pytest.raises(PublicChannelUnavailable):
        await view_public_channel(
            client,
            "channel",
            cancel_event=asyncio.Event(),
            on_progress=AsyncMock(),
            expected_channel_id=20,
        )
    assert client.requests == []


async def test_channel_pause_checked_before_any_batch_is_sent():
    from app.telegram.viewer import ChannelPaused

    client = Client()
    with pytest.raises(ChannelPaused):
        await view_public_channel(
            client,
            "channel",
            cancel_event=asyncio.Event(),
            on_progress=AsyncMock(),
            before_batch=AsyncMock(side_effect=ChannelPaused()),
        )
    assert client.requests == []
