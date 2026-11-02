import asyncio
from dataclasses import replace
import json
import unittest

import httpx

from ai_serving.backends.providers.backend import BackendHTTPError, BackendProtocolError, ModelCompatBackend
from ai_serving.contracts import ChatRequest
from ai_serving.testing import MockConfig, MockFault, MockServer


class MockServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = await MockServer(MockConfig(ttft=0, decode_delay=0, prefill_per_token=0)).start()
        self.backend = ModelCompatBackend(self.server.profile())

    async def asyncTearDown(self):
        await self.backend.aclose()
        await self.server.aclose()

    def request(self, **changes):
        return ChatRequest(messages=[{"role": "system", "content": "shared prefix " * 32},
                                     {"role": "user", "content": "hello"}], model="mock-model", **changes)

    async def test_stream_and_nonstream_have_same_content(self):
        chunks = [chunk async for chunk in self.backend.stream_chat(self.request())]
        async with httpx.AsyncClient() as client:
            response = await client.post(self.server.base_url + "/chat/completions", json={
                "model": "mock-model", "messages": self.request().messages, "stream": False})
        self.assertEqual("".join(chunk.text for chunk in chunks), response.json()["choices"][0]["message"]["content"])
        self.assertIsNotNone(chunks[-1].usage)
        self.assertEqual(chunks[-1].finish_reason, "stop")

    async def test_shared_prefix_reports_cached_tokens(self):
        cold = [chunk async for chunk in self.backend.stream_chat(self.request())]
        warm = [chunk async for chunk in self.backend.stream_chat(self.request())]
        self.assertEqual(cold[-1].usage["cached_tokens"], 0)
        self.assertGreater(warm[-1].usage["cached_tokens"], 0)
        self.assertEqual(self.server.stats.cache_hits, 1)

    async def test_json_schema_generates_parseable_fixture(self):
        schema = {"type": "object", "properties": {"action": {"enum": ["reply", "ignore"]},
                  "priority": {"type": "integer", "minimum": 1}}, "required": ["action", "priority"]}
        request = self.request(response_format={"type": "json_schema", "json_schema": {
            "name": "decision", "schema": schema, "strict": True}})
        chunks = [chunk async for chunk in self.backend.stream_chat(request)]
        self.assertEqual(json.loads("".join(chunk.text for chunk in chunks)), {"action": "reply", "priority": 1})

    async def test_429_and_5xx_are_classified(self):
        for status in (429, 503):
            self.server.queue_fault(status=status, retry_after=0.1)
            with self.assertRaises(BackendHTTPError) as raised:
                _ = [chunk async for chunk in self.backend.stream_chat(self.request())]
            self.assertEqual(raised.exception.status_code, status)
            self.assertTrue(raised.exception.classified.retry_candidate)
            self.assertEqual(raised.exception.classified.retry_after_seconds, 0.1)

    async def test_disconnect_is_an_incomplete_stream(self):
        self.server.queue_fault(disconnect_after=1)
        chunks = []
        with self.assertRaises(BackendProtocolError):
            async for chunk in self.backend.stream_chat(self.request()):
                chunks.append(chunk)
        self.assertEqual(len(chunks), 1)
        self.assertIsNone(chunks[0].usage)

    async def test_keepalive_hang_requires_progress_deadline(self):
        self.server.queue_fault(hang=True)
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(0.05):
                _ = [chunk async for chunk in self.backend.stream_chat(self.request())]
        await self.server.wait_idle()
        self.assertEqual(self.server.stats.cancelled, 1)
        self.assertEqual(self.server.stats.completed, 0)

    async def test_generator_close_stops_generation(self):
        self.server.config.decode_delay = 0.02
        stream = self.backend.stream_chat(self.request())
        await anext(stream)
        await stream.aclose()
        await self.server.wait_idle()
        self.assertEqual(self.server.stats.cancelled, 1)
        self.assertEqual(self.server.stats.active_requests, 0)

    async def test_idempotency_replays_and_rejects_key_reuse(self):
        self.backend.profile = replace(self.backend.profile, headers={"Idempotency-Key": "attempt-A"})
        first = [chunk async for chunk in self.backend.stream_chat(self.request())]
        second = [chunk async for chunk in self.backend.stream_chat(self.request())]
        self.assertEqual(first, second)
        self.assertEqual(self.server.stats.generations, 1)
        self.assertEqual(self.server.stats.idempotency_replays, 1)
        with self.assertRaises(BackendHTTPError) as raised:
            _ = [chunk async for chunk in self.backend.stream_chat(self.request(max_tokens=4))]
        self.assertEqual(raised.exception.status_code, 409)

    async def test_stats_do_not_retain_messages_or_keys(self):
        _ = [chunk async for chunk in self.backend.stream_chat(self.request())]
        self.assertTrue(all(type(value) is int for value in vars(self.server.stats).values()))
