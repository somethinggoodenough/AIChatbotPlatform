"""Real loopback model HTTP, SQLite and fake-channel acceptance tests."""

import ast
import asyncio
import inspect
import json
from pathlib import Path
import tempfile
import unittest

from ai_serving.backends.providers.backend import ModelCompatBackend, SGLangBackend
from ai_serving.channels.fake import FakeChannel
from ai_serving.contracts.messages import Message
from ai_serving.events.filtering import EventFilter, TokenBucket
from ai_serving.gateway.core import Assignment, Gateway
from ai_serving.runtime.router import router as router_module
from ai_serving.runtime.router.router import Router, parse_decision
from ai_serving.runtime.service import AgentRuntime
from ai_serving.runtime.workers.worker import Worker
from ai_serving.state.sqlite.store import StateStore
from ai_serving.testing.mock_server import MockConfig, MockServer
from ai_serving.tools.registry import default_tools
from ai_serving.observability.tracing import configure_tracing
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


class AgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.counts = {"router_decision": 0, "worker_step": 0, "reply": 0}
        self.saw_tools = False

        def respond(body):
            self.saw_tools |= "tools" in body
            name = body.get("response_format", {}).get("json_schema", {}).get("name", "reply")
            self.counts[name] += 1
            if name == "router_decision":
                needs_work = "lookup" in body["messages"][-1]["content"]
                return {"action": "spawn" if needs_work else "reply", "task": "lookup architecture" if needs_work else "",
                        "acceptance": ["Explain Router boundaries"] if needs_work else [], "task_id": None}
            if name == "worker_step":
                done = any(item["content"].startswith("Tool result (data):") for item in body["messages"])
                return {"action": "finish" if done else "tool", "tool": "rag_lookup", "arguments": {"query": "Router"},
                        "report": "The Router cannot execute tools." if done else "", "artifacts": ["architecture"] if done else []}
            return "Here is the reply."

        self.server = await MockServer(MockConfig(ttft=0, decode_delay=0, prefill_per_token=0, responder=respond)).start()
        self.backend = ModelCompatBackend(self.server.profile())
        self.gateway = Gateway({"mock": self.backend}, {"default": Assignment("mock")})
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name) / "state.db")
        self.channel = FakeChannel()
        self.runtime = self.make_runtime()

    def make_runtime(self):
        return AgentRuntime(
            Router(self.gateway, self.store), Worker(self.gateway, self.store, default_tools()),
            self.store, self.channel, EventFilter({"allowed"}, TokenBucket(1000)),
        )

    async def asyncTearDown(self):
        await self.gateway.aclose()
        await self.server.aclose()
        self.store.close()
        self.temp.cleanup()

    def message(self, number, text="hello", **kwargs):
        return Message(str(number), "user", "allowed", text, mentions_bot=True, **kwargs)

    async def test_chitchat_and_spawn_report_reply(self):
        await self.runtime.handle(self.message(1))
        self.assertEqual(self.counts, {"router_decision": 1, "worker_step": 0, "reply": 1})
        await self.runtime.handle(self.message(2, "lookup the architecture", thread_id="thread"))
        task = self.store.tasks()[0]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["artifacts"], ["architecture"])
        self.assertEqual(self.counts, {"router_decision": 2, "worker_step": 2, "reply": 2})
        self.assertEqual(len(self.channel.messages), 2)
        self.assertFalse(self.saw_tools)

    async def test_trace_covers_model_and_tool_without_message_content(self):
        exporter = InMemorySpanExporter()
        provider = configure_tracing(exporter)
        try:
            await self.runtime.handle(self.message("trace", "lookup secret-message-content"))
            spans = exporter.get_finished_spans()
            self.assertEqual(len({item.context.trace_id for item in spans}), 1)
            names = {item.name for item in spans}
            self.assertTrue({"request", "router.decision", "worker", "worker.tool", "model.call"} <= names)
            model_spans = [item for item in spans if item.name == "model.call"]
            self.assertTrue(all("backend" in item.attributes for item in model_spans))
            self.assertNotIn("secret-message-content", repr([dict(item.attributes) for item in spans]))
        finally:
            provider.shutdown()
            configure_tracing()

    async def test_thread_followup_skips_router_and_survives_restart(self):
        await self.runtime.handle(self.message(1, "lookup", thread_id="thread"))
        self.store.close()
        self.store = StateStore(Path(self.temp.name) / "state.db")
        self.runtime = self.make_runtime()
        followup = Message("2", "user", "allowed", "more details", thread_id="thread")
        await self.runtime.handle(followup)
        self.assertEqual(self.counts["router_decision"], 1)
        self.assertEqual(self.counts["worker_step"], 4)
        self.assertEqual(len(self.store.tasks()), 1)
        self.assertEqual(len(self.store.task_context(self.store.tasks()[0]["task_id"])), 2)

    async def test_other_user_cannot_drive_subscribed_task(self):
        await self.runtime.handle(self.message(1, "lookup", thread_id="thread"))
        before = self.counts.copy()
        result = await self.runtime.handle(Message("2", "other", "allowed", "change it", thread_id="thread"))
        self.assertIsNone(result)
        self.assertEqual(self.counts, before)

    async def test_three_hundred_mock_decisions_parse(self):
        router = self.runtime.router
        for offset in range(0, 300, 20):
            messages = [self.message(number) for number in range(offset, offset + 20)]
            for message in messages:
                self.store.save_message(message)
            decisions = await asyncio.gather(*(router.decide(message) for message in messages))
            self.assertTrue(all(decision.action == "reply" for decision in decisions))
        self.assertEqual(self.counts["router_decision"], 300)

    async def test_all_local_assignment_runs_same_flow(self):
        local = SGLangBackend(self.server.profile("local-sglang"))
        gateway = Gateway({"local": local}, {name: Assignment("local") for name in
                          ("default", "router_decision", "router_reply", "worker")})
        try:
            runtime = AgentRuntime(Router(gateway, self.store), Worker(gateway, self.store, default_tools()),
                                   self.store, self.channel, EventFilter({"allowed"}))
            await runtime.handle(self.message(1, "lookup"))
            self.assertEqual(self.store.tasks()[0]["status"], "completed")
        finally:
            await gateway.aclose()

    async def test_event_filter_and_duplicate_delivery_avoid_model_calls(self):
        for message in (
            Message("1", "user", "allowed", "hello", is_bot=True, mentions_bot=True),
            Message("2", "user", "other", "hello", mentions_bot=True),
            Message("3", "user", "allowed", "hello"),
            Message("4", "user", "allowed", "hello", reply_to="someone-else"),
        ):
            self.assertIsNone(await self.runtime.handle(message))
        self.assertEqual(self.server.stats.generations, 0)
        message = self.message(5)
        await self.runtime.handle(message)
        before = self.server.stats.generations
        await self.runtime.handle(message)
        self.assertEqual(self.server.stats.generations, before)

    async def test_stream_edits_are_throttled_and_long_replies_split(self):
        async def chunks():
            for character in "abcdefghijklmnopqrstuv":
                yield character

        channel = FakeChannel(edit_interval=60, max_length=10)
        await channel.publish(self.message(1), chunks())
        self.assertEqual("".join(item.text for item in channel.messages), "abcdefghijklmnopqrstuv")
        self.assertTrue(all(len(item.text) <= 10 for item in channel.messages))
        self.assertLessEqual(len(channel.edits), 3)

    def test_router_has_no_tool_execution_path(self):
        tree = ast.parse(inspect.getsource(router_module))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertFalse(any("workers" in name or ".tools" in name for name in imports))
        calls = [node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        self.assertNotIn("execute", calls)
        self.assertEqual(set(vars(self.runtime.router)), {"gateway", "store", "persona"})

    def test_token_bucket_refills_and_invalid_decisions_fail(self):
        bucket = TokenBucket(1, 0.5)
        self.assertTrue(bucket.allow("user", now=0))
        self.assertFalse(bucket.allow("user", now=1))
        self.assertTrue(bucket.allow("user", now=2))
        with self.assertRaises(ValueError):
            parse_decision(json.dumps({"action": "execute_tool"}))

    @unittest.skip("No real Discord connection is authorized; fake-channel acceptance runs above.")
    async def test_live_discord_private_server(self):
        pass


if __name__ == "__main__":
    unittest.main()
