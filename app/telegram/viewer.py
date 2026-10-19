from __future__ import annotations

from dataclasses import dataclass

from telethon import functions, types


class PublicChannelUnavailable(RuntimeError):
    pass


class ChannelPaused(PublicChannelUnavailable):
    pass


@dataclass(slots=True)
class ViewOutcome:
    available_posts: int
    viewed_posts: int
    last_message_id: int
    channel_title: str
    cancelled: bool = False
    channel_id: int | None = None
    scan_complete: bool = True


async def view_public_channel(
    client,
    username,
    *,
    start_after_id=0,
    cancel_event,
    on_progress,
    on_flood_wait=None,
    batch_size=100,
    expected_channel_id=None,
    on_resolve=None,
    before_batch=None,
    max_messages=None,
):
    entity = await client.get_entity(username)
    if not isinstance(entity, types.Channel) or not entity.broadcast:
        raise PublicChannelUnavailable("Это не публичный канал")
    if expected_channel_id and entity.id != expected_channel_id:
        raise PublicChannelUnavailable("Имя теперь принадлежит другому каналу. Проверьте ссылку.")
    if on_resolve:
        await on_resolve(entity.id)
    pending = []
    viewed = 0
    cursor = start_after_id
    processed = 0
    last_seen = cursor

    async def flush():
        nonlocal cursor, viewed
        if not pending or cancel_event.is_set():
            return
        if before_batch:
            await before_batch()
        await client(
            functions.messages.GetMessagesViewsRequest(
                peer=entity, id=list(pending), increment=True
            )
        )
        cursor = max(pending)
        count = len(pending)
        await on_progress(cursor, count)
        viewed += count
        pending.clear()

    async for message in client.iter_messages(
        entity, reverse=True, min_id=cursor, wait_time=1, limit=max_messages
    ):
        if cancel_event.is_set():
            break
        processed += 1
        last_seen = max(last_seen, message.id)
        if isinstance(message, types.Message) and message.id > cursor:
            pending.append(message.id)
            if len(pending) >= batch_size:
                await flush()
    await flush()
    if not cancel_event.is_set() and last_seen > cursor:
        await on_progress(last_seen, 0)
        cursor = last_seen
    return ViewOutcome(
        viewed + len(pending),
        viewed,
        cursor,
        entity.title or username,
        cancel_event.is_set(),
        entity.id,
        max_messages is None or processed < max_messages,
    )
