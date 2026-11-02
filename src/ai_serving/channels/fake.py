"""In-memory channel for offline acceptance tests; never contacts Discord."""

from dataclasses import dataclass
from typing import AsyncIterator

from ai_serving.contracts.messages import Message
from .streaming import publish_stream


@dataclass(slots=True)
class SentMessage:
    message_id: str
    channel_id: str
    thread_id: str | None
    text: str


class FakeChannel:
    def __init__(self, *, edit_interval: float = 0.5, max_length: int = 1900):
        self.messages: list[SentMessage] = []
        self.edits: list[tuple[str, str]] = []
        self.edit_interval = edit_interval
        self.max_length = max_length

    async def send(self, message: Message, text: str) -> str:
        message_id = str(len(self.messages) + 1)
        self.messages.append(SentMessage(message_id, message.channel_id, message.thread_id, text))
        return message_id

    async def edit(self, message_id: str, text: str) -> None:
        self.messages[int(message_id) - 1].text = text
        self.edits.append((message_id, text))

    async def publish(self, message: Message, chunks: AsyncIterator[str]) -> str:
        return await publish_stream(
            self, message, chunks, edit_interval=self.edit_interval, max_length=self.max_length,
        )
