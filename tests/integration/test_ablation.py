import unittest

from benchmarks.harness.ablation import run_once, summarize


class AblationTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_runtime_batching_and_passthrough_reduce_decisions(self):
        runs = [await run_once(configuration, seed=0, delay=0.01) for configuration in ("a", "b", "c")]
        normal, no_passthrough, no_batching = runs
        self.assertEqual(no_passthrough["router_decisions"] - normal["router_decisions"], 2)
        self.assertEqual(no_batching["router_decisions"] - normal["router_decisions"], 2)
        for row in runs:
            self.assertEqual(row["completed_tasks"], 3)
            self.assertEqual(row["cancelled_tasks"], 1)
            self.assertEqual(row["followups"], 2)
            self.assertEqual(len(row["task_latency_ms"]), 3)
            self.assertGreater(row["server"]["requests"], 0)
            self.assertGreater(row["prefix_cache_read_tokens"], 0)
            self.assertEqual(row["cost_per_completed_task_usd"], 0)

    async def test_single_session_delegation_changes_stop_responsiveness(self):
        neutral = await run_once("d-local", seed=0, delay=0.01)
        delegate = await run_once("e-api", seed=0, delay=0.01)
        self.assertEqual(neutral["delegation_rate"], 0.5)
        self.assertEqual(delegate["delegation_rate"], 1.0)
        # Verify effects of scheduling, not fragile absolute millisecond thresholds.
        self.assertEqual(neutral["cancelled_tasks"], 0)
        self.assertEqual(delegate["cancelled_tasks"], 1)
        self.assertEqual(neutral["completed_tasks"], 4)
        self.assertEqual(delegate["completed_tasks"], 3)
        self.assertEqual(summarize([neutral, delegate])[0]["runs"], 1)

    @unittest.skip("Real d/e model-delegation comparison requires GPU and authorized paid API; mock policies are scripted.")
    async def test_real_local_and_provider_delegation(self):
        pass
