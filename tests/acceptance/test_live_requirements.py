"""Real-system acceptance stays visible and skipped in the offline suite."""

import unittest


class LiveRequirements(unittest.TestCase):
    @unittest.skip("No GPU: real SGLang prefix cache, schema and server-side abort not verified")
    def test_sglang_gpu_acceptance(self):
        pass

    @unittest.skip("Paid API calls prohibited: provider compatibility, billing and abort not verified")
    def test_paid_provider_acceptance(self):
        pass

    @unittest.skip("Fake channel only: real Discord delivery, permissions and rate limits not verified")
    def test_real_discord_acceptance(self):
        pass

    @unittest.skip("No GPU: comparison with real SGLang Model Gateway cache_aware not run")
    def test_sglang_gateway_baseline(self):
        pass

    @unittest.skip("Real model judge prohibited: red-team quality and judge calibration unverified")
    def test_live_red_team_judge(self):
        pass

    @unittest.skip("No GPU/paid API: model-dependent delegation in ablations d/e unverified")
    def test_live_ablation_models(self):
        pass
