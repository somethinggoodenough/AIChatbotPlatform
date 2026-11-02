"""Channel orchestration lives outside the tool-free Router."""

import asyncio

from ai_serving.contracts.messages import ChannelAdapter, Message, WorkerReport
from ai_serving.events.filtering import EventFilter
from ai_serving.observability.tracing import traced
from ai_serving.runtime.router.router import Router
from ai_serving.runtime.workers.worker import Worker
from ai_serving.state.sqlite.store import StateStore


class AgentRuntime:
    def __init__(self, router: Router, worker: Worker, store: StateStore,
                 channel: ChannelAdapter, event_filter: EventFilter):
        self.router = router
        self.worker = worker
        self.store = store
        self.channel = channel
        self.event_filter = event_filter
        self._router_lock = asyncio.Lock()

    @traced("request")
    async def handle(self, message: Message) -> str | None:
        subscribed = self.store.subscription(message.channel_id, message.thread_id)
        if not self.event_filter.accepts(message, subscribed=subscribed is not None):
            return None
        if not self.store.save_message(message):
            return None
        report = None
        if subscribed:
            if self.store.task(subscribed)["user_id"] != message.user_id:
                return None
            self.store.append_task_message(subscribed, message)
            report = await self.worker.run(subscribed)
        else:
            async with self._router_lock:
                decision = await self.router.decide(message)
            if decision.action == "ignore":
                return None
            if decision.action == "spawn":
                task_id = self.store.create_task(message, decision.task, decision.acceptance)
                report = await self.worker.run(task_id)
            elif decision.action == "forward_to_worker":
                task = self.store.task(decision.task_id)
                if task["user_id"] != message.user_id or task["session_id"] != message.session_id:
                    report = WorkerReport(decision.task_id, "Task does not belong to this session.", accepted=False)
                else:
                    self.store.append_task_message(decision.task_id, message)
                    report = await self.worker.run(decision.task_id)
        return await self.channel.publish(message, self.router.reply(message, report))

    async def run(self, messages) -> None:
        """The input loop stays responsive while Workers await models or tools."""
        async with asyncio.TaskGroup() as group:
            async for message in messages:
                group.create_task(self.handle(message))
