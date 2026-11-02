"""Turn token streaming into bounded, throttled channel message edits."""

import time
from typing import AsyncIterator, Protocol

from ai_serving.contracts.messages import Message


class MessageSink(Protocol):
    async def send(self, message: Message, text: str) -> str: ...
    async def edit(self, message_id: str, text: str) -> None: ...


async def publish_stream(
    sink: MessageSink, message: Message, chunks: AsyncIterator[str],
    *, edit_interval: float = 0.5, max_length: int = 1900,
) -> str:
    """Flush on completion, splitting long replies into separate messages."""
    text = ""
    sent_text = ""
    reply_id = ""
    first_id = ""
    last_edit = 0.0
    async for chunk in chunks:
        text += chunk
        while len(text) > max_length:
            part, text = text[:max_length], text[max_length:]
            if reply_id:
                await sink.edit(reply_id, part)
            else:
                reply_id = await sink.send(message, part)
            first_id = first_id or reply_id
            reply_id = ""
            sent_text = ""
        if text and not reply_id:
            reply_id = await sink.send(message, text)
            first_id = first_id or reply_id
            sent_text = text
            last_edit = time.monotonic()
        elif text and time.monotonic() - last_edit >= edit_interval:
            await sink.edit(reply_id, text)
            sent_text = text
            last_edit = time.monotonic()
    if reply_id and text != sent_text:
        await sink.edit(reply_id, text)
    return first_id
