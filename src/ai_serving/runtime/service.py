"""Durable channel orchestration lives outside the tool-free Router."""

import asyncio
from dataclasses import asdict
import json
import time
import uuid

from ai_serving.contracts import ChatRequest
from ai_serving.contracts.messages import ChannelAdapter, Message, WorkerReport
from ai_serving.events.filtering import EventFilter
from ai_serving.observability.tracing import traced
from ai_serving.runtime.router.router import Router, parse_decision
from ai_serving.runtime.recovery.journal import supports_step_replay
from ai_serving.runtime.workers.worker import TaskBusy, Worker
from ai_serving.state.sqlite.store import LeaseLost, StateStore


async def one_chunk(text):
    yield text


class AgentRuntime:
    def __init__(self, router: Router, worker: Worker, store: StateStore,
                 channel: ChannelAdapter, event_filter: EventFilter, *, thread_passthrough: bool = True):
        self.router = router
        self.worker = worker
        self.store = store
        self.channel = channel
        self.event_filter = event_filter
        self.thread_passthrough = thread_passthrough
        self._router_lock = asyncio.Lock()
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._active: dict[str, asyncio.Task] = {}

    async def stop(self, task_id: str, *, user_id: str) -> bool:
        if not self.store.stop(task_id, user_id):
            return False
        running = self._active.get(task_id)
        if running:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        return True

    @traced("request")
    async def handle(self, message: Message) -> str | None:
        subscribed = self.store.subscription(message.channel_id, message.thread_id)
        if not self.event_filter.accepts(message, subscribed=subscribed is not None):
            return None
        if subscribed and self.store.task(subscribed)["user_id"] != message.user_id:
            return None
        if not self.store.enqueue(message):
            return None
        return await self._process(message)

    @traced("request.batch")
    async def handle_batch(self, messages: list[Message]) -> str | None:
        """A batch shares a session and makes one decision over its saved context."""
        if len({(item.session_id, item.user_id) for item in messages}) > 1:
            raise ValueError("a batch must belong to one session and user")
        accepted = []
        for message in messages:
            subscribed = self.store.subscription(message.channel_id, message.thread_id)
            if subscribed and self.store.task(subscribed)["user_id"] != message.user_id:
                continue
            if subscribed and self.thread_passthrough:
                await self.handle(message)
            elif self.event_filter.accepts(message) and self.store.enqueue(message):
                accepted.append(message)
        if not accepted:
            return None
        self.store.group_inbox(accepted)
        return await self._process(accepted[-1])

    async def _renew_inbox(self, message_id, owner, token, task):
        while True:
            await asyncio.sleep(5)
            try:
                self.store.inbox_update(message_id, owner, token, lease_until=time.time() + 15)
            except LeaseLost:
                task.cancel("Inbox lease lost")
                return

    async def _run_worker(self, task_id: str) -> WorkerReport:
        lock = self._task_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            task = asyncio.create_task(self.worker.run(task_id))
            self._active[task_id] = task
            try:
                return await task
            finally:
                self._active.pop(task_id, None)

    async def _process(self, message: Message) -> str | None:
        owner = uuid.uuid4().hex
        token = self.store.inbox_claim(message.message_id, owner, 15)
        if token is None:
            return None
        renew = asyncio.create_task(self._renew_inbox(message.message_id, owner, token, asyncio.current_task()))

        def update(**values):
            if values == {"status": "done"}:
                self.store.acknowledge_inbox(message.message_id, owner, token)
            else:
                self.store.inbox_update(message.message_id, owner, token, **values)

        task_id = None
        try:
            saved = self.store.inbox(message.message_id)
            subscribed = self.store.subscription(message.channel_id, message.thread_id)
            if message.text.strip().lower() == "/stop" and subscribed:
                await self.stop(subscribed, user_id=message.user_id)
                reply_id = await self.channel.publish(message, one_chunk("Task stopped."))
                update(status="done")
                return reply_id
            if saved["status"] == "reply_started":
                # A crash can leave an unacknowledged partial channel delivery.
                # Do not call the model or repost the answer invisibly.
                update(status="uncertain")
                return None
            report = None
            task_id = saved["task_id"]
            if task_id:
                report = await self._run_worker(task_id)
            elif subscribed and self.thread_passthrough:
                self.store.append_task_message(subscribed, message)
                task_id = subscribed
                update(status="routed", task_id=task_id)
                report = await self._run_worker(task_id)
            else:
                async with self._router_lock:
                    if saved["decision"]:
                        decision = parse_decision(saved["decision"])
                    else:
                        request = ChatRequest(**json.loads(saved["decision_input"])) if saved["decision_input"] else self.router.prepare_decision(message)
                        if saved["status"] == "decision_started" and not supports_step_replay(self.router.gateway, request):
                            update(status="uncertain")
                            return None
                        update(status="decision_started", decision_input=json.dumps(asdict(request)))
                        decision = await self.router.decide(message, request)
                        update(status="decided", decision=json.dumps(asdict(decision)))
                if decision.action == "ignore":
                    update(status="done")
                    return None
                if decision.action == "spawn":
                    task_id = self.store.create_task(message, decision.task, decision.acceptance)
                elif decision.action == "forward_to_worker":
                    task = self.store.task(decision.task_id)
                    if task["user_id"] != message.user_id or task["session_id"] != message.session_id:
                        report = WorkerReport(decision.task_id, "Task does not belong to this session.", accepted=False)
                    else:
                        task_id = decision.task_id
                        self.store.append_task_message(task_id, message)
                if task_id:
                    update(status="routed", task_id=task_id)
                    report = await self._run_worker(task_id)
            update(status="reply_started")
            reply_id = await self.channel.publish(message, self.router.reply(message, report))
            update(status="done")
            return reply_id
        except TaskBusy:
            # Another process owns the task; the durable inbox remains resumable.
            return None
        except asyncio.CancelledError:
            if task_id and self.store.task(task_id)["status"] == "cancelled":
                update(status="cancelled")
                return None
            raise
        finally:
            renew.cancel()
            await asyncio.gather(renew, return_exceptions=True)
            try:
                update(lease_until=0)
            except LeaseLost:
                pass

    async def resume_pending(self) -> None:
        """Restart from the persisted inbox and saved decisions, not model guesses."""
        await asyncio.gather(*(self._process(message) for message in self.store.pending_messages()))
        # A timeout may have already produced a channel report. Retry the task's
        # incomplete round independently; completed model steps remain replayed.
        for task in self.store.tasks(status="pending"):
            try:
                await self._run_worker(task["task_id"])
            except TaskBusy:
                continue

    async def run(self, messages) -> None:
        async with asyncio.TaskGroup() as group:
            async for message in messages:
                group.create_task(self.handle(message))
