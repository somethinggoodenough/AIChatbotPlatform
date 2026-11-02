import unittest

from ai_serving.backends.providers.backend import SGLangBackend
from ai_serving.testing import MockConfig, MockServer

from benchmarks.harness.baseline import run_case


class BenchmarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_prefix_measurements_come_from_http_backend(self):
        row = await run_case(2, 4, "shared_prefix", 7, (4,), (4,))
        self.assertEqual(row["server"]["requests"], 4)
        self.assertEqual(row["server"]["completed"], 4)
        self.assertGreater(row["cache_read_tokens"], 0)
        self.assertGreater(row["ttft_p50_ms"], 0)
        self.assertGreater(row["output_tokens_per_second"], 0)

    async def test_judgment_workload_parses_structured_response(self):
        row = await run_case(1, 2, "judgment", 7, (4,), (16,))
        self.assertEqual(row["server"]["completed"], 2)

    async def test_external_sglang_backend_uses_same_measurements_and_stays_open(self):
        async with MockServer(MockConfig(ttft=0, decode_delay=0)) as server:
            backend = SGLangBackend(server.profile("local-sglang"))
            try:
                row = await run_case(1, 2, "judgment", 7, (4,), (16,), backend)
                self.assertIsNone(row["server"])
                self.assertEqual(row["model"], "mock-model")
                self.assertEqual(server.stats.requests, 2)
                self.assertGreater(row["output_tokens"], 0)
                another = await run_case(1, 2, "judgment", 7, (4,), (16,), backend)
                self.assertEqual(another["samples"][0]["usage"]["cached_tokens"], 0)
                self.assertGreater(another["samples"][1]["usage"]["cached_tokens"], 0)
                self.assertTrue(await backend.health())
            finally:
                await backend.aclose()

    @unittest.skip("Real SGLang latency baseline requires the unavailable GPU; --local is opt-in after deployment.")
    async def test_real_sglang_baseline(self):
        pass
