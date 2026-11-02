"""Channel-independent messages and routing decisions."""

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol


@dataclass(frozen=True, slots=True)
class Message:
    message_id: str
    user_id: str
    channel_id: str
    text: str
    thread_id: str | None = None
    attachments: tuple[str, ...] = ()
    mentions_bot: bool = False
    reply_to: str | None = None
    is_bot: bool = False

    @property
    def session_id(self) -> str:
        return f"{self.channel_id}:{self.thread_id or self.user_id}"


class ChannelAdapter(Protocol):
    async def publish(self, message: Message, chunks: AsyncIterator[str]) -> str: ...


@dataclass(frozen=True, slots=True)
class Decision:
    action: str
    task: str = ""
    acceptance: tuple[str, ...] = ()
    task_id: str | None = None


@dataclass(slots=True)
class WorkerReport:
    task_id: str
    text: str
    artifacts: list[str] = field(default_factory=list)
    accepted: bool = True
