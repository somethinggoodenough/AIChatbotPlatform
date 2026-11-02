"""Coalesce related arrivals into one callback, without duplicate execution."""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ai_serving.contracts.messages import Message


@dataclass
class Pending:
    messages: list[Message] = field(default_factory=list)
    futures: list[asyncio.Future] = field(default_factory=list)


class EventBatcher:
    def __init__(self, callback: Callable[[list[Message]], Awaitable[Any]], window: float = 0.01):
        self.callback = callback
        self.window = window
        self.pending: dict[tuple, Pending] = {}
        self.tasks: set[asyncio.Task] = set()

    async def submit(self, message: Message):
        if self.window <= 0:
            return await self.callback([message])
        key = (message.channel_id, message.thread_id, message.user_id)
        if key not in self.pending:
            self.pending[key] = Pending()
            task = asyncio.create_task(self._flush(key))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        future = asyncio.get_running_loop().create_future()
        self.pending[key].messages.append(message)
        self.pending[key].futures.append(future)
        return await future

    async def _flush(self, key: tuple) -> None:
        await asyncio.sleep(self.window)
        pending = self.pending.pop(key)
        live = [(message, future) for message, future in zip(pending.messages, pending.futures)
                if not future.cancelled()]
        if not live:
            return
        try:
            result = await self.callback([message for message, _ in live])
        except BaseException as exc:
            for _, future in live:
                if not future.done():
                    future.set_exception(exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
        else:
            for _, future in live:
                if not future.done():
                    future.set_result(result)

    async def aclose(self) -> None:
        for pending in self.pending.values():
            for future in pending.futures:
                future.cancel()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
