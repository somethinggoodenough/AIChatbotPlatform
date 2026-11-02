"""Failure policy uses real loopback HTTP streams, never a hosted provider."""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
from decimal import Decimal
import tempfile
from time import monotonic
import unittest

import httpx

from ai_serving.backends.providers import BackendProtocolError, ModelCompatBackend
from ai_serving.api import ApiServer
from ai_serving.backends.providers.usage import PriceTable
from ai_serving.contracts import ChatRequest
from ai_serving.contracts.messages import Message
from ai_serving.events.batching import EventBatcher
from ai_serving.gateway.accounting import CostLedger
from ai_serving.gateway.routing import BackendPolicy, RetryContext, Route, RoutedGateway, RoutingExhausted
from ai_serving.testing.mock_server import MockConfig, MockFault, MockServer


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = AsyncExitStack()
        self.servers = {}
        self.backends = {}
        for name in ("primary", "secondary", "local"):
            server = await self.stack.enter_async_context(MockServer(MockConfig(
                ttft=0.001, decode_delay=0.001, response_text=f"{name} answer",
            )))
            self.servers[name] = server
            self.backends[name] = ModelCompatBackend(server.profile(name))
        self.policy = BackendPolicy(first_timeout=2, idle_timeout=2,
                                    max_retries=0, backoff=0.001, jitter=0,
                                    failure_threshold=1, reset_after=0.1)
        self.gateway = RoutedGateway(self.backends, {"default": Route(
            ("primary",), ("secondary", "local"), canned_reply="Temporarily unavailable.",
        )}, policy=self.policy)
        self.request = ChatRequest([{"role": "system", "content": "Stable prefix. " * 64},
                                    {"role": "user", "content": "hello"}], "configured")
        self.context = RetryContext(retry_safe=True, user_id="user", task_id="task")

    async def asyncTearDown(self):
        await self.gateway.aclose()
        await self.stack.aclose()

    async def test_rate_limit_respects_retry_after_and_records_unknown_attempt(self):
        self.gateway.policy = replace(self.policy, max_retries=1, failure_threshold=3)
        self.servers["primary"].queue_fault(status=429, retry_after=0.03)
        started = monotonic()
        result = await self.gateway.chat(self.request, self.context)
        self.assertEqual(result.text, "primary answer")
        self.assertGreaterEqual(monotonic() - started, 0.03)
        self.assertEqual(self.servers["primary"].stats.requests, 2)
        self.assertEqual(self.gateway.ledger.totals(user_id="user")["unknown_attempts"], 1)

    async def test_default_context_never_repeats_generation(self):
        self.servers["primary"].queue_fault(status=503)
        result = await self.gateway.chat(self.request)
        self.assertEqual(result.text, "Temporarily unavailable.")
        self.assertEqual(self.servers["primary"].stats.requests, 1)
        self.assertEqual(self.servers["secondary"].stats.requests, 0)

    async def test_fallback_ladder_uses_separate_fixed_prompts(self):
        self.servers["primary"].config.fault = MockFault(status=503)
        self.servers["secondary"].config.fault = MockFault(status=503)
        captured = []

        def responder(body):
            captured.append(body)
            return "local result"

        self.servers["local"].config.responder = responder
        self.gateway.routes["default"] = Route(("primary",), ("secondary", "local"),
            prompts={"primary": "Primary prompt", "secondary": "Secondary prompt", "local": "Short local prompt"})
        result = await self.gateway.chat(self.request, self.context)
        self.assertEqual(result.text, "local result")
        self.assertEqual(captured[0]["messages"][0]["content"], "Short local prompt")
        self.assertEqual(captured[0]["messages"][-1]["role"], "user")
        self.assertEqual(self.gateway.ledger.totals(task_id="task")["attempts"], 3)

    async def test_keepalive_hang_hits_first_content_deadline(self):
        self.gateway.backend_policies["primary"] = replace(self.policy, first_timeout=0.05)
        self.servers["primary"].config.fault = MockFault(hang=True)
        result = await self.gateway.chat(self.request, self.context)
        self.assertEqual(result.text, "secondary answer")
        await self.servers["primary"].wait_idle()
        self.assertEqual(self.servers["primary"].stats.cancelled, 1)

    async def test_idle_timeout_after_first_text_never_retries(self):
        self.gateway.backend_policies["primary"] = replace(self.policy, idle_timeout=0.05)
        self.servers["primary"].config.decode_delay = 1
        seen = []
        with self.assertRaises(TimeoutError):
            async for chunk in self.gateway.stream_chat(self.request, self.context):
                seen.append(chunk.text)
        self.assertTrue(any(seen))
        self.assertEqual(self.servers["secondary"].stats.requests, 0)
        await self.servers["primary"].wait_idle()

    async def test_reasoning_output_also_prevents_retry(self):
        self.servers["primary"].config.reasoning = "reasoning fragment"
        self.servers["primary"].queue_fault(disconnect_after=0)
        seen = []
        with self.assertRaises(BackendProtocolError):
            async for chunk in self.gateway.stream_chat(self.request, self.context):
                seen.append(chunk.reasoning)
        self.assertIn("reasoning fragment", seen)
        self.assertEqual(self.servers["secondary"].stats.requests, 0)

    async def test_total_deadline_bounds_retries_and_returns_static_busy(self):
        self.gateway.policy = replace(self.policy, first_timeout=1)
        self.gateway.total_timeout = 0.03
        self.gateway.routes["default"] = Route(("primary",), ("secondary",))
        self.servers["primary"].config.fault = MockFault(hang=True)
        started = monotonic()
        result = await self.gateway.chat(self.request, self.context)
        self.assertIn("busy", result.text)
        self.assertLess(monotonic() - started, 0.5)
        self.assertEqual(self.servers["secondary"].stats.requests, 0)

    async def test_circuit_skips_unhealthy_backend_and_recovers_with_probe(self):
        self.servers["primary"].queue_fault(status=503)
        await self.gateway.chat(self.request, self.context)
        self.assertGreater(self.gateway.circuits["primary"].open_until, 0)
        self.gateway.circuits["primary"].open_until = monotonic() + 60
        await self.gateway.chat(self.request, self.context)
        self.assertEqual(self.servers["primary"].stats.requests, 1)
        self.gateway.circuits["primary"].open_until = monotonic() - 1
        result = await self.gateway.chat(self.request, self.context)
        self.assertEqual(result.text, "primary answer")
        self.assertEqual(self.gateway.circuits["primary"].failures, 0)

    async def test_prefix_affinity_reuses_warm_replica(self):
        self.gateway.routes["default"] = Route(("primary", "secondary"))
        first = await self.gateway.chat(self.request, self.context)
        second = await self.gateway.chat(self.request, self.context)
        self.assertEqual(first.usage["cached_tokens"], 0)
        self.assertGreater(second.usage["cached_tokens"], 0)
        self.assertEqual(self.servers["secondary"].stats.requests, 0)

    async def test_budget_and_structured_output_never_return_fake_decision(self):
        self.gateway.max_attempts = 1
        self.servers["primary"].queue_fault(status=503)
        req = replace(self.request, response_format={"type": "json_schema", "json_schema": {
            "name": "decision", "schema": {"type": "object", "properties": {}}}})
        with self.assertRaises(RoutingExhausted):
            await self.gateway.chat(req, self.context)
        self.assertEqual(self.servers["secondary"].stats.requests, 0)

    async def test_cancelled_task_does_not_fallback(self):
        self.servers["primary"].config.fault = MockFault(hang=True)
        task = asyncio.create_task(self.gateway.chat(self.request, self.context))
        async with asyncio.timeout(1):
            while not self.servers["primary"].stats.active_requests:
                await asyncio.sleep(0.001)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.servers["primary"].wait_idle()
        self.assertEqual(self.servers["secondary"].stats.requests, 0)

    async def test_costs_persist_per_user_task_with_decimal_prices(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = CostLedger(directory + "/costs.sqlite")
            self.gateway.ledger.close()
            self.gateway.ledger = ledger
            prices = PriceTable(input_per_million=Decimal("1"), output_per_million=Decimal("2"),
                                cache_read_per_million=Decimal("0.1"), cache_write_per_million=Decimal("1"))
            self.backends["primary"].profile = replace(self.backends["primary"].profile, prices=prices)
            context = replace(self.context, request_id="same-user-request")
            await self.gateway.chat(self.request, context)
            await self.gateway.chat(self.request, context)
            before = ledger.totals(user_id="user", task_id="task")
            self.assertEqual(before["attempts"], 2)
            self.assertGreater(Decimal(before["known_cost"]["USD"]), 0)
            self.assertEqual(ledger.request_cost("same-user-request"), Decimal(before["known_cost"]["USD"]))
            ledger.close()
            self.gateway.ledger = CostLedger(directory + "/costs.sqlite")
            self.assertEqual(self.gateway.ledger.totals(user_id="user"), before)

    async def test_api_reports_actual_fallback_model(self):
        self.servers["primary"].config.fault = MockFault(status=503)
        self.backends["secondary"].profile = replace(self.backends["secondary"].profile, model="secondary-model")
        async with ApiServer(self.gateway) as api:
            async with httpx.AsyncClient(base_url=api.base_url) as client:
                body = {"model": "configured", "messages": [], "retry_safe": True}
                response = await client.post("/v1/chat/completions", json=body)
                self.assertEqual(response.json()["model"], "secondary-model")
                stream = await client.post("/v1/chat/completions", json={**body, "stream": True})
                self.assertIn('"model": "secondary-model"', stream.text)


class BatchingTests(unittest.IsolatedAsyncioTestCase):
    async def test_arrivals_coalesce_once_and_other_users_remain_separate(self):
        batches = []

        async def callback(messages):
            batches.append(messages)
            return len(messages)

        batcher = EventBatcher(callback, window=0.01)
        messages = [Message(str(index), "user", "channel", str(index)) for index in range(6)]
        try:
            results = await asyncio.gather(*(batcher.submit(message) for message in messages))
            self.assertEqual(results, [6] * 6)
            self.assertEqual(len(batches), 1)
            await asyncio.gather(batcher.submit(messages[0]), batcher.submit(replace(messages[0], user_id="other")))
            self.assertEqual(len(batches), 3)
        finally:
            await batcher.aclose()

    async def test_cancelled_pending_message_is_not_executed(self):
        batches = []

        async def callback(messages):
            batches.extend(messages)

        batcher = EventBatcher(callback, window=0.02)
        task = asyncio.create_task(batcher.submit(Message("1", "u", "c", "hello")))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0.03)
        self.assertEqual(batches, [])
        await batcher.aclose()
