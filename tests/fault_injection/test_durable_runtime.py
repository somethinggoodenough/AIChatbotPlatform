"""Crash windows, fencing and replay against the real loopback mock server."""

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import time
import unittest

from ai_serving.backends.providers.backend import ModelCompatBackend
from ai_serving.channels.fake import FakeChannel
from ai_serving.contracts.messages import Message, WorkerReport
from ai_serving.events.filtering import EventFilter, TokenBucket
from ai_serving.gateway.core import Assignment, Gateway
from ai_serving.runtime.recovery.journal import Journal, RecoveryMetrics
from ai_serving.runtime.recovery.watchdog import Watchdog
from ai_serving.runtime.router.router import Router
from ai_serving.runtime.service import AgentRuntime
from ai_serving.runtime.workers.worker import TaskBusy, Worker
from ai_serving.state.sqlite.store import LeaseLost, StateStore
from ai_serving.testing.idempotent_backend import MockIdempotentBackend
from ai_serving.testing.mock_server import MockConfig, MockServer
from ai_serving.tools.registry import Tool, ToolContext, ToolRegistry, default_tools, effect_tool


def effect_response(body):
    name = body.get("response_format", {}).get("json_schema", {}).get("name")
    if name == "router_decision":
        return {"action": "spawn", "task": "record one effect", "acceptance": ["report_contains:done"], "task_id": None}
    if name == "worker_step":
        done = any(message["content"].startswith("Tool result (data):") for message in body["messages"])
        return {"action": "finish" if done else "tool", "tool": "record_effect",
                "arguments": {"name": "counter", "delta": 1}, "report": "done" if done else "", "artifacts": []}
    return "The task finished."


class DurableRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.db"
        self.store = StateStore(self.path)
        self.server = await MockServer(MockConfig(ttft=0, decode_delay=0, prefill_per_token=0,
                                                  responder=effect_response)).start()
        self.backend = MockIdempotentBackend(self.server.profile())
        self.gateway = Gateway({"mock": self.backend}, {"default": Assignment("mock")})

    async def asyncTearDown(self):
        await self.gateway.aclose()
        await self.server.aclose()
        self.store.close()
        self.temp.cleanup()

    def task(self, number=1):
        message = Message(str(number), "user", "channel", "record effect", thread_id=f"thread-{number}", mentions_bot=True)
        self.store.save_message(message)
        return self.store.create_task(message, "record effect", ("report_contains:done",)), message

    def worker(self, **kwargs):
        return Worker(self.gateway, self.store, ToolRegistry([effect_tool()]), **kwargs)

    def runtime(self, worker=None):
        return AgentRuntime(Router(self.gateway, self.store), worker or self.worker(), self.store,
                            FakeChannel(), EventFilter({"channel"}, TokenBucket(1000)))

    async def test_lease_claim_is_exclusive_and_old_fence_cannot_write(self):
        task_id, _ = self.task()
        other = StateStore(self.path)
        try:
            lease = self.store.claim(task_id, "first", 0.03)
            self.assertIsNone(other.claim(task_id, "second", 1))
            await asyncio.sleep(0.04)
            self.assertIn(task_id, Watchdog(self.store).expired_tasks())
            newer = other.claim(task_id, "second", 1)
            self.assertGreater(newer.token, lease.token)
            for operation in (
                lambda: self.store.heartbeat(lease, 1),
                lambda: self.store.effect(lease, "stale", "counter", 1),
                lambda: self.store.complete(lease, WorkerReport(task_id, "done")),
                lambda: self.store.finish_step(lease, 0, "stale"),
            ):
                with self.assertRaises(LeaseLost):
                    operation()
            self.assertEqual(other.effect(newer, "new", "counter", 1)["value"], 1)
        finally:
            other.close()

    async def test_concurrent_worker_does_not_repeat_task(self):
        task_id, _ = self.task()
        self.server.config.ttft = 0.05
        first = asyncio.create_task(self.worker().run(task_id))
        while not self.server.stats.active_requests:
            await asyncio.sleep(0.001)
        with self.assertRaises(TaskBusy):
            await self.worker().run(task_id)
        self.assertTrue((await first).accepted)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM tool_receipts").fetchone()[0], 1)

    async def test_completed_task_and_followup_use_distinct_journal_rounds(self):
        task_id, message = self.task()
        await self.worker().run(task_id)
        count = self.server.stats.generations
        await self.worker().run(task_id)
        self.assertEqual(self.server.stats.generations, count)
        followup = Message("followup", "user", "channel", "another effect", thread_id=message.thread_id)
        self.store.append_task_message(task_id, followup)
        await self.worker().run(task_id)
        self.assertEqual(self.store.connection.execute("SELECT value FROM effects WHERE name = 'counter'").fetchone()[0], 2)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM rounds").fetchone()[0], 2)

    async def test_unknown_model_outcome_is_not_reissued(self):
        task_id, _ = self.task()

        class Crash(BaseException):
            pass

        def crash(stage, kind, index):
            if stage == "after_call" and kind == "model":
                raise Crash()

        with self.assertRaises(Crash):
            await self.worker(crash_hook=crash).run(task_id)
        non_idempotent = ModelCompatBackend(self.server.profile())
        gateway = Gateway({"mock": non_idempotent}, {"default": Assignment("mock")})
        try:
            count = self.server.stats.generations
            report = await Worker(gateway, self.store, ToolRegistry([effect_tool()])).run(task_id)
            self.assertFalse(report.accepted)
            self.assertEqual(self.store.task(task_id)["status"], "uncertain")
            self.assertEqual(self.server.stats.generations, count)
        finally:
            await gateway.aclose()

    async def test_compensation_reverses_executed_order_once(self):
        task_id, _ = self.task()
        lease = self.store.claim(task_id, "owner", 5)
        journal = Journal(self.store, lease, RecoveryMetrics())
        tool = effect_tool()
        registry = ToolRegistry([tool])
        for delta in (1, 2):
            arguments = {"name": "counter", "delta": delta}

            async def apply(key):
                return await registry.execute(tool.name, arguments, context=ToolContext(self.store, lease, key))

            await journal.call("tool", tool.name, arguments, apply, replay_safe=True, compensation=tool.compensation(arguments))
        worker = self.worker()
        await worker.rollback(lease)
        await worker.rollback(lease)
        rows = self.store.connection.execute("SELECT input FROM tool_receipts ORDER BY rowid").fetchall()
        self.assertEqual([json.loads(row[0])["delta"] for row in rows], [1, 2, -2, -1])
        self.assertEqual(self.store.connection.execute("SELECT value FROM effects").fetchone()[0], 0)

    async def test_failed_followup_does_not_compensate_successful_prior_round(self):
        task_id, message = self.task()
        await self.worker().run(task_id)
        self.store.append_task_message(task_id, Message("follow", "user", "channel", "again", thread_id=message.thread_id))
        self.server.config.responder = lambda body: {"action": "tool", "tool": "record_effect",
                                                     "arguments": {"name": "counter", "delta": 1}, "report": "", "artifacts": []}
        report = await self.worker().run(task_id)
        self.assertFalse(report.accepted)
        self.assertEqual(self.store.connection.execute("SELECT value FROM effects").fetchone()[0], 1)
        self.assertEqual(self.store.step(task_id, 1)["compensated"], 0)

    def test_followup_insert_and_revision_are_one_transaction(self):
        task_id, message = self.task()
        self.store.connection.execute("CREATE TEMP TRIGGER fail_revision BEFORE UPDATE OF revision ON tasks BEGIN SELECT RAISE(ABORT, 'test interruption'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append_task_message(task_id, Message("follow", "user", "channel", "again", thread_id=message.thread_id))
        self.assertEqual(len(self.store.task_context(task_id)), 1)
        self.assertEqual(self.store.task(task_id)["revision"], 1)
        self.store.connection.execute("DROP TRIGGER fail_revision")
        self.store.append_task_message(task_id, Message("follow", "user", "channel", "again", thread_id=message.thread_id))
        self.assertEqual(self.store.task(task_id)["revision"], 2)

    async def test_logic_failure_replans_from_checkpoint(self):
        task_id, _ = self.task()

        def response(body):
            repaired = any("Re-plan from" in item["content"] for item in body["messages"])
            return {"action": "finish", "tool": "", "arguments": {},
                    "report": "done" if repaired else "missing required result", "artifacts": []}

        self.server.config.responder = response
        report = await self.worker().run(task_id)
        self.assertTrue(report.accepted)
        self.assertEqual(self.server.stats.generations, 2)
        self.assertEqual(self.store.step(task_id, 1)["kind"], "checkpoint")

    async def test_batch_rejects_cross_user_context_and_nonowner_followup(self):
        runtime = self.runtime()
        with self.assertRaises(ValueError):
            await runtime.handle_batch([Message("a", "one", "channel", "a", mentions_bot=True),
                                        Message("b", "two", "channel", "b", mentions_bot=True)])
        task_id, message = self.task()
        runtime.thread_passthrough = False
        result = await runtime.handle_batch([Message("other", "other", "channel", "change", thread_id=message.thread_id, mentions_bot=True)])
        self.assertIsNone(result)
        self.assertEqual(self.server.stats.generations, 0)

    async def test_persisted_batch_resumes_one_representative_and_acknowledges_members(self):
        messages = [Message(f"batch-{number}", "user", "channel", "record", mentions_bot=True) for number in range(3)]
        for message in messages:
            self.store.enqueue(message)
        self.store.group_inbox(messages)
        self.assertEqual([item.message_id for item in self.store.pending_messages()], [messages[-1].message_id])
        self.assertIsNone(self.store.inbox_claim(messages[0].message_id, "competing", 5))
        await self.runtime().resume_pending()
        self.assertEqual(len(self.store.tasks()), 1)
        self.assertTrue(all(self.store.inbox(item.message_id)["status"] == "done" for item in messages))
        self.assertEqual(self.server.stats.generations, 4)

    async def test_expired_inbox_token_cannot_commit(self):
        message = Message("expired", "user", "channel", "test")
        self.store.enqueue(message)
        token = self.store.inbox_claim(message.message_id, "first", 0.01)
        await asyncio.sleep(0.02)
        with self.assertRaises(LeaseLost):
            self.store.inbox_update(message.message_id, "first", token, status="done")

    async def test_watchdog_hung_tool_and_repeating_plan_report_failure(self):
        entered_tool = asyncio.Event()
        async def hang(arguments):
            entered_tool.set()
            await asyncio.Event().wait()

        task_id, _ = self.task()
        self.server.config.responder = lambda body: {"action": "tool", "tool": "hang", "arguments": {}, "report": "", "artifacts": []}
        started = time.monotonic()
        worker = Worker(self.gateway, self.store, ToolRegistry([Tool("hang", "test hang", hang)]),
                        progress_timeout=0.5, lease_seconds=0.15)
        report = await worker.run(task_id)
        self.assertFalse(report.accepted)
        self.assertIn("progress deadline", report.text)
        self.assertTrue(entered_tool.is_set())
        self.assertLess(time.monotonic() - started, 2)
        next_task, _ = self.task(2)
        self.server.config.responder = lambda body: {"action": "tool", "tool": "add", "arguments": {"a": 1, "b": 1}, "report": "", "artifacts": []}
        report = await Worker(self.gateway, self.store, default_tools()).run(next_task)
        self.assertFalse(report.accepted)
        self.assertIn("repeating", report.text)

    async def test_read_only_timeout_can_resume_without_repeating_completed_model_call(self):
        task_id, _ = self.task()

        def response(body):
            done = any(item["content"].startswith("Tool result (data):") for item in body["messages"])
            return {"action": "finish" if done else "tool", "tool": "read", "arguments": {},
                    "report": "done" if done else "", "artifacts": []}

        async def hang(arguments):
            # Inject a read timeout after the model step, independent of CPU load.
            raise TimeoutError("synthetic read timeout")

        async def healthy(arguments):
            return "available"

        self.server.config.responder = response
        failed = await Worker(self.gateway, self.store, ToolRegistry([Tool("read", "read data", hang)]),
                              progress_timeout=2).run(task_id)
        self.assertFalse(failed.accepted)
        self.assertEqual(self.store.task(task_id)["status"], "pending")
        recovered = Worker(self.gateway, self.store, ToolRegistry([Tool("read", "read data", healthy)]))
        self.assertTrue((await recovered.run(task_id)).accepted)
        self.assertEqual(self.server.stats.generations, 2)
        self.assertEqual(recovered.metrics.steps_replayed, 1)

    async def test_router_restart_reuses_saved_decisions_and_preserves_subscriptions(self):
        messages = [Message(str(number), "user", "channel", "record", thread_id=f"thread-{number}", mentions_bot=True)
                    for number in range(12)]
        for message in messages:
            self.store.enqueue(message)
            token = self.store.inbox_claim(message.message_id, "old-router", 5)
            self.store.inbox_update(message.message_id, "old-router", token, status="decided", lease_until=0,
                                    decision=json.dumps({"action": "spawn", "task": "record one effect",
                                                         "acceptance": ["report_contains:done"], "task_id": None}))
        self.store.close()
        self.store = StateStore(self.path)
        runtime = self.runtime()
        await runtime.resume_pending()
        self.assertEqual(len(self.store.tasks(status="completed")), 12)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM subscriptions").fetchone()[0], 12)
        self.assertEqual(len(self.store.pending_messages()), 0)
        self.assertEqual(self.server.stats.generations, 36)  # two Worker calls + one reply; no Router decision replay

    async def test_router_uncertain_decision_replays_only_verified_mock_request(self):
        message = Message("decision-crash", "user", "channel", "record", mentions_bot=True)
        runtime = self.runtime()
        self.store.enqueue(message)
        request = runtime.router.prepare_decision(message)
        token = self.store.inbox_claim(message.message_id, "old", 5)
        self.store.inbox_update(message.message_id, "old", token, status="decision_started",
                                decision_input=json.dumps(asdict(request)), lease_until=0)
        await runtime.router.decide(message, request)  # response is lost before the inbox decision write
        await runtime.resume_pending()
        self.assertEqual(self.server.stats.idempotency_replays, 1)
        self.assertEqual(self.store.inbox(message.message_id)["status"], "done")

    async def test_stop_revokes_lease_and_closes_model_stream(self):
        self.server.config.ttft = 0.05
        runtime = self.runtime()
        message = Message("start", "user", "channel", "record", thread_id="thread", mentions_bot=True)
        running = asyncio.create_task(runtime.handle(message))
        while not self.store.tasks():
            await asyncio.sleep(0.001)
        await runtime.handle(Message("stop", "user", "channel", "/stop", thread_id="thread"))
        await running
        await self.server.wait_idle()
        self.assertEqual(self.store.tasks()[0]["status"], "cancelled")
        self.assertEqual(runtime.channel.messages[-1].text, "Task stopped.")

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "Actual kill -9 crash injection requires POSIX SIGKILL")
    async def test_sigkill_at_each_model_and_tool_boundary(self):
        for kind in ("model", "tool"):
            for stage in ("before_call", "after_call", "after_journal"):
                with self.subTest(kind=kind, stage=stage):
                    task_id, _ = self.task(f"{kind}-{stage}")
                    before_generations = self.server.stats.generations
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, "-m", "ai_serving.testing.crash_worker", "--database", str(self.path),
                        "--task-id", task_id, "--base-url", self.server.base_url, "--kind", kind, "--stage", stage,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    async with asyncio.timeout(10):
                        stdout, stderr = await process.communicate()
                    self.assertEqual(process.returncode, -signal.SIGKILL, stderr.decode())
                    while self.store.task(task_id)["lease_until"] > time.time():
                        await asyncio.sleep(0.01)
                    worker = self.worker()
                    report = await worker.run(task_id)
                    self.assertTrue(report.accepted)
                    receipts = self.store.connection.execute("SELECT count(*) FROM tool_receipts WHERE task_id = ?", (task_id,)).fetchone()[0]
                    self.assertEqual(receipts, 1)
                    self.assertEqual(self.server.stats.generations - before_generations, 2)
                    self.assertEqual(self.store.task(task_id)["status"], "completed")
                    if kind == "tool" or stage == "after_journal":
                        self.assertGreater(worker.metrics.steps_replayed, 0)
                        self.assertGreater(worker.metrics.tokens_avoided, 0)


if __name__ == "__main__":
    unittest.main()
