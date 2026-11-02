"""Public Chat API cannot bypass guards through internal routing call types."""

from argparse import Namespace
from contextlib import AsyncExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from ai_serving.api import ApiServer
from ai_serving.api.__main__ import build_gateway
from ai_serving.backends.providers import ModelCompatBackend
from ai_serving.gateway import Assignment, Gateway
from ai_serving.guards.pipeline import GuardedGateway, GuardPolicy, ModelChecks
from ai_serving.testing import MockConfig, MockServer


ROOT = Path(__file__).resolve().parents[2]


class GuardedApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = AsyncExitStack()
        self.main = await self.stack.enter_async_context(MockServer(MockConfig(response_text="safe answer")))
        self.checks = await self.stack.enter_async_context(MockServer(MockConfig(
            responder=lambda body: {"allow": True}, ttft=0, decode_delay=0,
        )))
        raw = Gateway({"main": ModelCompatBackend(self.main.profile("main")),
                       "classifier": ModelCompatBackend(self.checks.profile("classifier"))},
                      {"default": Assignment("main"), "input_classifier": Assignment("classifier"),
                       "topic_classifier": Assignment("classifier")})
        self.guard = GuardedGateway(raw, ModelChecks(raw), policy=GuardPolicy(classifier_timeout=1))
        self.stack.push_async_callback(self.guard.aclose)
        self.api = await self.stack.enter_async_context(ApiServer(self.guard))
        self.client = await self.stack.enter_async_context(httpx.AsyncClient(base_url=self.api.base_url))

    async def asyncTearDown(self):
        await self.stack.aclose()

    async def test_structured_rejection_is_403_and_cancels_hanging_model(self):
        self.main.queue_fault(hang=True)
        self.checks.config.responder = lambda body: {"allow": False}
        response = await self.client.post("/v1/chat/completions", json={
            "model": "configured", "messages": [{"role": "user", "content": "private-input"}],
            "stream": True, "response_format": {"type": "json_schema", "json_schema": {
                "name": "answer", "schema": {"type": "object", "properties": {"answer": {"type": "string"}},
                                             "required": ["answer"]}}},
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"error": {"type": "guard_rejected"}})
        self.assertNotIn("private-input", response.text)
        self.assertNotIn(self.guard.canary, response.text)
        await self.main.wait_idle()
        self.assertEqual(self.main.stats.cancelled, 1)

    async def test_allowed_request_exposes_usage_and_content_free_metrics(self):
        response = await self.client.post("/v1/chat/completions", json={
            "model": "configured", "messages": [{"role": "user", "content": "private-input"}],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "safe answer")
        self.assertGreater(response.json()["usage"]["completion_tokens"], 0)
        metrics = (await self.client.get("/metrics")).text
        self.assertIn("model_ttft_seconds", metrics)
        self.assertNotIn("private-input", metrics)
        self.assertNotIn(self.guard.canary, metrics)
        self.assertEqual(self.checks.stats.generations, 2)

    async def test_public_call_type_cannot_select_unguarded_internal_route(self):
        for call_type in ("router_decision", "input_classifier", "topic_classifier"):
            response = await self.client.post("/v1/chat/completions", json={
                "model": "configured", "messages": [], "call_type": call_type,
            })
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.main.stats.generations, 0)
        self.assertEqual(self.checks.stats.generations, 0)

    async def test_cli_setup_uses_separate_mock_classifier_and_persistent_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            args = Namespace(local=False, routing=ROOT / "configs/policies/routing.toml", config=None,
                             guards=ROOT / "configs/policies/guards.toml",
                             budget_db=Path(directory) / "budget.sqlite")
            canary = None
            for _ in range(2):
                async with AsyncExitStack() as stack:
                    with patch("ai_serving.api.__main__.read_environment", side_effect=AssertionError("offline setup read .env")):
                        gateway = await build_gateway(args, stack)
                    self.assertEqual(gateway.gateway.routes["input_classifier"].primary, ("classifier",))
                    self.assertEqual(gateway.gateway.routes["topic_classifier"].primary, ("classifier",))
                    self.assertTrue(all(backend.profile.base_url.startswith("http://127.0.0.1:")
                                        for backend in gateway.backends.values()))
                    if canary:
                        self.assertEqual(gateway.canary, canary)
                    canary = gateway.canary
            self.assertTrue(args.budget_db.exists())
