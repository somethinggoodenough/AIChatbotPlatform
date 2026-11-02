"""Leased Worker execution, durable steps and bounded logic re-planning."""

import asyncio
from dataclasses import asdict
import json
import time
import uuid

from ai_serving.backends.providers.usage import TokenUsage
from ai_serving.contracts import ChatRequest
from ai_serving.contracts.messages import WorkerReport
from ai_serving.gateway.routing import RetryContext
from ai_serving.observability.tracing import traced
from ai_serving.runtime.recovery.journal import Journal, RecoveryMetrics, UncertainOutcome, supports_step_replay
from ai_serving.runtime.recovery.watchdog import RepeatedAction, Watchdog
from ai_serving.state.sqlite.store import LeaseLost, StateStore
from ai_serving.tools.registry import ToolContext, ToolRegistry


WORKER_FORMAT = {
    "type": "json_schema", "json_schema": {"name": "worker_step", "schema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["tool", "finish"]},
            "tool": {"type": "string"}, "arguments": {"type": "object"},
            "report": {"type": "string"}, "artifacts": {"type": "array", "items": {"type": "string"}},
        }, "required": ["action", "tool", "arguments", "report", "artifacts"],
    }},
}


def parse_step(text: str) -> dict:
    step = json.loads(text)
    if not isinstance(step, dict) or set(step) != {"action", "tool", "arguments", "report", "artifacts"}:
        raise ValueError("invalid Worker step fields")
    if step["action"] not in {"tool", "finish"} or not isinstance(step["arguments"], dict):
        raise ValueError("invalid Worker action")
    if not isinstance(step["tool"], str) or not isinstance(step["report"], str):
        raise ValueError("invalid Worker output")
    if not isinstance(step["artifacts"], list) or not all(isinstance(item, str) for item in step["artifacts"]):
        raise ValueError("invalid Worker artifacts")
    return step


def check_acceptance(criteria: list[str], report: WorkerReport) -> None:
    if not report.text.strip():
        raise ValueError("report is empty")
    for item in criteria:
        if item.startswith("report_contains:") and item.partition(":")[2].strip() not in report.text:
            raise ValueError("report is missing required text")
        if item.startswith("artifact:") and item.partition(":")[2].strip() not in report.artifacts:
            raise ValueError("report is missing a required artifact")


class TaskBusy(RuntimeError):
    pass


class Worker:
    def __init__(self, gateway, store: StateStore, tools: ToolRegistry, *, max_steps: int = 8,
                 lease_seconds: float = 5.0, progress_timeout: float = 30.0,
                 repeat_limit: int = 3, crash_hook=None, model_retry_safe: bool = False):
        self.gateway = gateway
        self.store = store
        self.tools = tools
        self.max_steps = max_steps
        self.lease_seconds = lease_seconds
        self.progress_timeout = progress_timeout
        self.repeat_limit = repeat_limit
        self.crash_hook = crash_hook
        self.model_retry_safe = model_retry_safe
        self.metrics = RecoveryMetrics()

    async def _heartbeat(self, lease, owner_task, watchdog):
        while True:
            await asyncio.sleep(self.lease_seconds / 3)
            try:
                self.store.heartbeat(lease, self.lease_seconds)
                if watchdog.stalled(lease.task_id):
                    owner_task.cancel("Worker progress deadline exceeded")
                    return
            except LeaseLost:
                owner_task.cancel("Worker lease lost")
                return

    async def rollback(self, lease) -> None:
        for step in self.store.compensations(lease.task_id, lease.start_step):
            action = step["compensation"]
            # The built-in sink can distinguish a crash before and after the effect.
            receipt = self.store.connection.execute(
                "SELECT 1 FROM tool_receipts WHERE idempotency_key = ?", (step["idempotency_key"],),
            ).fetchone()
            if step["status"] == "completed" or receipt:
                context = ToolContext(self.store, lease, "compensate:" + step["idempotency_key"])
                await self.tools.execute(action["tool"], action["arguments"], context=context)
            self.store.mark_compensated(lease, step["step_index"])

    async def _round(self, lease, watchdog) -> WorkerReport:
        task = self.store.task(lease.task_id)
        # A leased revision identifies a fixed input turn, even if new messages arrive.
        turn = self.store.connection.execute(
            "SELECT message_id FROM task_messages WHERE task_id=? ORDER BY rowid LIMIT 1 OFFSET ?",
            (lease.task_id, lease.revision - 1),
        ).fetchone()[0]
        journal = Journal(self.store, lease, self.metrics, self.crash_hook)
        context = [{"role": "system", "content":
                    "Complete this task and return a report that satisfies its acceptance criteria.\n"
                    + json.dumps({"available_tools": self.tools.descriptions()})},
                   {"role": "user", "content": "Task specification:\n" + json.dumps({
                       "task": task["spec"], "acceptance": task["acceptance"],
                   })}]
        context += self.store.task_context(lease.task_id, lease.revision)
        for _ in range(self.max_steps):
            request = ChatRequest(messages=context.copy(), model="configured", call_type="worker",
                                  request_id=f"{lease.task_id}:{journal.index}",
                                  response_format=WORKER_FORMAT, temperature=None)
            backend, _ = self.gateway.select(request)

            async def model_call(key):
                result = await self.gateway.chat(request, RetryContext(
                    task_id=lease.task_id, user_id=task["user_id"], step_index=journal.index, request_id=key,
                    session_id=task["session_id"], turn_id=turn, retry_safe=self.model_retry_safe,
                ))
                output = asdict(result)
                usage = result.usage or {}
                price = backend.profile.prices.estimate(TokenUsage(
                    input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                    cache_read_tokens=usage.get("cached_tokens"), cache_write_tokens=usage.get("cache_write_tokens"),
                    reasoning_tokens=usage.get("reasoning_tokens"),
                ))
                ledger = getattr(self.gateway, "ledger", None)
                if ledger is not None:
                    price = ledger.request_cost(key)
                output["cost"] = float(price) if price is not None else None
                return output

            async with asyncio.timeout(self.progress_timeout):
                result = await journal.call("model", backend.name, asdict(request), model_call,
                                            replay_safe=supports_step_replay(self.gateway, request))
            context.append({"role": "assistant", "content": result["text"]})
            try:
                step = parse_step(result["text"])
                if step["action"] == "finish":
                    report = WorkerReport(lease.task_id, step["report"], step["artifacts"])
                    check_acceptance(task["acceptance"], report)
                    return report
                tool = self.tools.tools[step["tool"]]
                signature = json.dumps({"tool": tool.name, "arguments": step["arguments"]}, sort_keys=True)
                watchdog.observe_action(signature)

                async def tool_call(key):
                    return await self.tools.execute(tool.name, step["arguments"],
                                                    context=ToolContext(self.store, lease, key))

                compensation = tool.compensation(step["arguments"]) if tool.compensation else None
                async with asyncio.timeout(self.progress_timeout):
                    output = await journal.call("tool", tool.name, step["arguments"], tool_call,
                                                replay_safe=True, compensation=compensation)
                context.append({"role": "user", "content": "Tool result (data):\n" + json.dumps(output)})
            except RepeatedAction:
                raise
            except (ValueError, KeyError, TypeError) as exc:
                error = {"error": type(exc).__name__, "reason": str(exc)}

                async def checkpoint(key):
                    return error

                await journal.call("checkpoint", "logic", error, checkpoint, replay_safe=True)
                context.append({"role": "user", "content": "Re-plan from the last completed step: " + json.dumps(error)})
        raise RepeatedAction("Worker exceeded its step budget")

    @traced("worker")
    async def run(self, task_id: str) -> WorkerReport:
        while True:
            started = time.monotonic()
            task = self.store.task(task_id)
            if task["cancel_requested"]:
                return WorkerReport(task_id, "Task stopped.", accepted=False)
            if task["completed_revision"] >= task["revision"]:
                return WorkerReport(task_id, task["report"] or "", task["artifacts"], task["status"] == "completed")
            lease = self.store.claim(task_id, uuid.uuid4().hex, self.lease_seconds)
            if lease is None:
                raise TaskBusy("task is leased or requires explicit recovery")
            watchdog = Watchdog(self.store, progress_timeout=self.progress_timeout, repeat_limit=self.repeat_limit)
            heartbeat = asyncio.create_task(self._heartbeat(lease, asyncio.current_task(), watchdog))
            try:
                report = await self._round(lease, watchdog)
                self.store.complete(lease, report)
            except UncertainOutcome:
                report = WorkerReport(task_id, "A previous model call may have completed. Recovery needs provider reconciliation.", accepted=False)
                self.store.complete(lease, report, status="uncertain")
            except (RepeatedAction, ValueError, KeyError, TypeError):
                await self.rollback(lease)
                report = WorkerReport(task_id, "Worker stopped after an invalid or repeating plan.", accepted=False)
                self.store.complete(lease, report)
            except TimeoutError:
                report = WorkerReport(task_id, "Worker stopped after its progress deadline.", accepted=False)
                self.store.pause(lease, report.text)
            except asyncio.CancelledError:
                if self.store.task(task_id)["cancel_requested"]:
                    raise
                if watchdog.stalled(task_id):
                    report = WorkerReport(task_id, "Worker stopped after its progress deadline.", accepted=False)
                    self.store.pause(lease, report.text)
                else:
                    raise
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                self.store.release(lease)
                self.metrics.seconds += time.monotonic() - started
            latest = self.store.task(task_id)
            if latest["status"] != "completed" or latest["revision"] == lease.revision:
                return report
