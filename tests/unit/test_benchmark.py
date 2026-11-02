import unittest

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
