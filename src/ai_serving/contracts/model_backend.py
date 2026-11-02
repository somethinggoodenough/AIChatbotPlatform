"""Canonical model backend contract used by the gateway and agent runtime."""

from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol, runtime_checkable


@dataclass(slots=True)
class ChatRequest:
    messages: list[dict[str, Any]]
    model: str
    max_tokens: int = 512
    temperature: float | None = None
    response_format: dict[str, Any] | None = None
    call_type: str = "default"
    request_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.messages, list) or not all(isinstance(item, dict) for item in self.messages):
            raise TypeError("messages must be a list of dictionaries")
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("model must be a nonempty string")
        if type(self.max_tokens) is not int or self.max_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer")
        if self.temperature is not None and (
            type(self.temperature) not in {int, float} or not 0 <= self.temperature <= 2
        ):
            raise ValueError("temperature must be between 0 and 2")
        if self.response_format is not None and not isinstance(self.response_format, dict):
            raise TypeError("response_format must be a dictionary or None")
        if not isinstance(self.call_type, str) or not self.call_type:
            raise ValueError("call_type must be a nonempty string")
        if not isinstance(self.request_id, str):
            raise TypeError("request_id must be a string")


@dataclass(slots=True)
class ChatChunk:
    text: str = ""
    finish_reason: str | None = None
    # Present only on the terminal chunk. Values use canonical token names.
    usage: dict[str, int | None] | None = None
    reasoning: str = ""
    # Actual selected model; static fallback replies use "canned" or "busy".
    model: str | None = None


@runtime_checkable
class ModelBackend(Protocol):
    name: str

    def stream_chat(self, req: ChatRequest) -> AsyncIterator[ChatChunk]: ...

    async def chat(self, req: ChatRequest) -> ChatChunk: ...

    async def health(self) -> bool: ...
