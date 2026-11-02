"""Cheap event filtering before model calls."""

from dataclasses import dataclass
import time

from ai_serving.contracts.messages import Message


@dataclass(slots=True)
class Bucket:
    tokens: float
    updated: float


class TokenBucket:
    def __init__(self, capacity: int = 10, refill_per_second: float = 1.0):
        if capacity <= 0 or refill_per_second < 0:
            raise ValueError("invalid token bucket limits")
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._users: dict[str, Bucket] = {}

    def allow(self, user_id: str, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        bucket = self._users.setdefault(user_id, Bucket(float(self.capacity), now))
        bucket.tokens = min(self.capacity, bucket.tokens + (now - bucket.updated) * self.refill_per_second)
        bucket.updated = now
        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        return True


class EventFilter:
    def __init__(self, allowed_channels: set[str], limiter: TokenBucket | None = None):
        self.allowed_channels = allowed_channels
        self.limiter = limiter or TokenBucket()

    def accepts(self, message: Message, *, subscribed: bool = False) -> bool:
        if message.is_bot or message.channel_id not in self.allowed_channels:
            return False
        if not subscribed and not message.mentions_bot:
            return False
        return self.limiter.allow(message.user_id)
