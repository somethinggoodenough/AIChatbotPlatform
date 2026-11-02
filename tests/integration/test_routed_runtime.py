"""Agent flow through the routing policy, durable ledger and event batching."""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from ai_serving.backends.providers import ModelCompatBackend
from ai_serving.backends.providers.usage import PriceTable
from ai_serving.channels.fake import FakeChannel
from ai_serving.contracts.messages import Message
from ai_serving.events.batching import EventBatcher
from ai_serving.gateway.routing import BackendPolicy, Route, RoutedGateway
from ai_serving.runtime.demo import build_runtime, demo_response
from ai_serving.state.sqlite.store import StateStore
from ai_serving.testing import MockConfig, MockFault, MockServer


class RoutedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_spawn_followup_fallback_and_actual_cost_accounting(self):
        counts = {"router_decision": 0, "worker_step": 0}

        def respond(body):
            kind = body.get("response_format", {}).get("json_schema", {}).get("name")
            if kind in counts:
                counts[kind] += 1
            return demo_response(body)

        with tempfile.TemporaryDirectory() as directory:
            async with AsyncExitStack() as stack:
                primary = await stack.enter_async_context(MockServer(MockConfig(fault=MockFault(status=503))))
                secondary = await stack.enter_async_context(MockServer(MockConfig(
                    responder=respond, ttft=0, decode_delay=0, prefill_per_token=0,
                )))
                prices = PriceTable(input_per_million=Decimal(2), output_per_million=Decimal(4),
                                    cache_read_per_million=Decimal(1), cache_write_per_million=Decimal(2))
                gateway = RoutedGateway({
                    "primary": ModelCompatBackend(primary.profile("primary")),
                    "secondary": ModelCompatBackend(replace(secondary.profile("secondary"), prices=prices)),
                }, {"default": Route(("primary",), ("secondary",))},
                    policy=BackendPolicy(max_retries=0, failure_threshold=1, reset_after=10))
                stack.push_async_callback(gateway.aclose)
                calls = []
                original = gateway.stream_chat

                def capture(request, context=None):
                    calls.append(context)
                    return original(request, context)

                gateway.stream_chat = capture
                store = StateStore(Path(directory) / "state.sqlite")
                stack.callback(store.close)
                channel = FakeChannel()
                runtime = build_runtime(gateway, store, channel, ["demo"], model_retry_safe=True)
                batcher = EventBatcher(runtime.handle_batch, window=0.01)
                stack.push_async_callback(batcher.aclose)
                await asyncio.gather(*(batcher.submit(Message(
                    str(i), "alice", "demo", "lookup Router", thread_id="work", mentions_bot=True,
                )) for i in range(3)))
                self.assertEqual(counts["router_decision"], 1)
                self.assertEqual(len(store.tasks()), 1)
                self.assertEqual(len(channel.messages), 1)
                self.assertEqual(primary.stats.requests, 1)
                task = store.tasks()[0]
                self.assertEqual(task["status"], "completed")
                totals = gateway.ledger.totals(task_id=task["task_id"], user_id="alice")
                self.assertGreater(Decimal(totals["known_cost"]["USD"]), 0)
                self.assertEqual(totals["unknown_attempts"], 0)
                worker_calls = [context for context in calls if context.step_index is not None]
                self.assertTrue(all(context.session_id == "demo:work" for context in worker_calls))
                self.assertEqual({context.turn_id for context in worker_calls}, {"2"})
                await runtime.handle(Message("followup", "alice", "demo", "more detail", thread_id="work"))
                self.assertEqual(counts["router_decision"], 1)
                self.assertEqual(len(channel.messages), 2)
                self.assertEqual(store.tasks()[0]["revision"], 2)
                self.assertEqual(calls[-1].turn_id, "followup")
                self.assertNotIn("lookup Router", "\n".join(gateway.ledger.db.iterdump()))
