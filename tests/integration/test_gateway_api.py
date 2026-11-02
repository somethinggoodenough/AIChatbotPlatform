"""The same backend client talks to the mock directly and through our API."""

import asyncio
from dataclasses import replace
from pathlib import Path
import unittest

import httpx

from ai_serving.api import ApiServer
from ai_serving.backends.providers import ModelCompatBackend
from ai_serving.contracts import ChatRequest
from ai_serving.gateway import Assignment, Gateway, load_assignments
from ai_serving.testing.mock_server import MockConfig, MockServer


ROOT = Path(__file__).resolve().parents[2]


class GatewayApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mock = await MockServer(MockConfig(response_text="A mock answer", reasoning="Consider it.")).start()
        self.backend = ModelCompatBackend(self.mock.profile())
        self.gateway = Gateway({"mock": self.backend}, load_assignments(ROOT / "configs/models/mock.toml"))
        self.api = await ApiServer(self.gateway).__aenter__()
        self.client = httpx.AsyncClient(base_url=self.api.base_url)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.api.__aexit__(None, None, None)
        await self.gateway.aclose()
        await self.mock.aclose()

    async def test_nonstream_reasoning_usage_and_metrics(self):
        response = await self.client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "not-in-metrics"}], "model": "configured",
        })
        self.assertEqual(response.status_code, 200)
        answer = response.json()
        self.assertEqual(answer["choices"][0]["message"]["content"], "A mock answer")
        self.assertEqual(answer["choices"][0]["message"]["reasoning_content"], "Consider it.")
        self.assertGreater(answer["usage"]["completion_tokens"], 0)
        metrics = (await self.client.get("/metrics")).text
        self.assertIn("model_ttft_seconds", metrics)
        self.assertIn("model_inter_chunk_seconds", metrics)
        self.assertIn("gateway_overhead_seconds", metrics)
        self.assertIn("model_provider_seconds", metrics)
        self.assertIn("model_cost_total", metrics)
        self.assertNotIn("not-in-metrics", metrics)

    async def test_same_backend_client_streams_through_gateway_api(self):
        profile = replace(self.mock.profile(), base_url=self.api.base_url + "/v1")
        client_backend = ModelCompatBackend(profile)
        try:
            chunks = [item async for item in client_backend.stream_chat(ChatRequest([], "mock-model"))]
        finally:
            await client_backend.aclose()
        self.assertEqual("".join(item.text for item in chunks), "A mock answer")
        self.assertEqual("".join(item.reasoning for item in chunks), "Consider it.")
        self.assertEqual(chunks[-1].finish_reason, "stop")
        self.assertIsNotNone(chunks[-1].usage)

    async def test_call_type_selects_configured_model(self):
        requests = []

        def answer(body):
            requests.append(body)
            return "ok"

        self.mock.config.responder = answer
        self.gateway.assignments["router_decision"] = Assignment("mock", "judge-model")
        await self.gateway.chat(ChatRequest([], "ignored", call_type="router_decision"))
        self.assertEqual(requests[0]["model"], "judge-model")
        self.assertNotIn("temperature", requests[0])

    async def test_http_error_is_classified_without_leaking_input(self):
        self.mock.queue_fault(status=429)
        response = await self.client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "secret text"}], "model": "mock-model",
        })
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["type"], "rate_limit")
        self.assertNotIn("secret", response.text)
        self.assertIn("model_errors_total", self.gateway.metrics.render())

    async def test_client_disconnect_cancels_upstream_keepalive_hang(self):
        self.mock.queue_fault(hang=True)
        reader, writer = await asyncio.open_connection("127.0.0.1", self.api.port)
        body = b'{"messages":[],"model":"mock-model","stream":true}'
        writer.write(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                     + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()
        async with asyncio.timeout(2):
            while not self.mock.stats.active_requests:
                await asyncio.sleep(0.001)
        writer.close()
        await writer.wait_closed()
        await self.mock.wait_idle()
        self.assertEqual(self.mock.stats.cancelled, 1)

    async def test_stream_does_not_claim_done_after_upstream_disconnect(self):
        self.mock.queue_fault(disconnect_after=1)
        response = await self.client.post("/v1/chat/completions", json={
            "messages": [], "model": "mock-model", "stream": True,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("upstream_error", response.text)
        self.assertNotIn("[DONE]", response.text)

    async def test_unsupported_tools_are_explicitly_rejected(self):
        response = await self.client.post("/v1/chat/completions", json={
            "messages": [], "model": "mock-model", "tools": [],
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.mock.stats.requests, 0)

    async def test_all_local_assignments_use_only_local_backend(self):
        assignments = load_assignments(ROOT / "configs/models/all_local.toml")
        self.assertEqual({item.backend for item in assignments.values()}, {"local"})
        self.assertTrue({"router_decision", "router_reply", "worker"} <= assignments.keys())


if __name__ == "__main__":
    unittest.main()
