"""Offline tests: no provider calls, credentials or GPU required."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from ai_serving.backends.providers.errors import ErrorClassifier
from ai_serving.backends.providers.profile import Capabilities, RequestMapping, UnsupportedCapability
from ai_serving.backends.providers.usage import PriceTable, TokenUsage, UsageAccumulator, UsageNormalizer
from ai_serving.config.providers import ProfileConfigError, load_active_profile, load_profile, read_environment


ROOT = Path(__file__).resolve().parents[2]
ENV = {
    "SGLANG_BASE_URL": "http://localhost:30000/v1", "SGLANG_MODEL": "test-local",
    "DEEPSEEK_BASE_URL": "https://example.invalid", "DEEPSEEK_MODEL": "test-api",
    "DEEPSEEK_API_KEY": "test-secret-never-log",
}


class ProviderProfileTests(unittest.TestCase):
    def setUp(self):
        self.local = load_profile(ROOT / "configs/providers/local_sglang.toml", ENV)
        self.deepseek = load_profile(ROOT / "configs/providers/deepseek.toml", ENV)

    def test_same_caller_switches_provider_through_config(self):
        for profile, model, url in (
            (self.local, "test-local", "http://localhost:30000/v1/chat/completions"),
            (self.deepseek, "test-api", "https://example.invalid/chat/completions"),
        ):
            body = profile.build_request([{"role": "user", "content": "hello"}], stream=True, max_output_tokens=32)
            self.assertEqual(body["model"], model)
            self.assertEqual(body["max_tokens"], 32)
            self.assertEqual(body["stream_options"], {"include_usage": True})
            self.assertEqual(profile.url, url)

    def test_json_schema_not_silently_downgraded(self):
        schema = {"type": "object", "properties": {"action": {"type": "string"}}}
        body = self.local.build_request([], json_schema=schema)
        self.assertEqual(body["response_format"]["json_schema"]["schema"], schema)
        self.assertNotIn("strict", body["response_format"]["json_schema"])
        with self.assertRaises(UnsupportedCapability):
            self.deepseek.build_request([], json_schema=schema)
        with self.assertRaises(UnsupportedCapability):
            self.local.build_request([], json_schema=schema, strict_schema=True)

    def test_reasoning_and_token_field_are_loaded_from_configuration(self):
        source = (ROOT / "configs/providers/deepseek.toml").read_text()
        source = source.replace('max_tokens_field = "max_tokens"', 'max_tokens_field = "max_completion_tokens"')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.toml"
            path.write_text(source)
            profile = load_profile(path, ENV)
        body = profile.build_request([], reasoning="high", max_output_tokens=16)
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertEqual(body["max_completion_tokens"], 16)
        self.assertNotIn("max_tokens", body)
        self.assertEqual(profile.build_request([], reasoning="none")["thinking"], {"type": "disabled"})
        with self.assertRaises(UnsupportedCapability):
            profile.build_request([], reasoning="unmapped")

    def test_capabilities_reject_unsupported_tools_and_streaming(self):
        with self.assertRaises(UnsupportedCapability):
            self.deepseek.build_request([], tools=[])
        self.assertEqual(self.deepseek.build_request([], temperature=0)["temperature"], 0)
        profile = replace(self.deepseek, capabilities=Capabilities())
        with self.assertRaises(UnsupportedCapability):
            profile.build_request([], stream=True)

    def test_stream_usage_flag_only_on_streaming_requests(self):
        profile = replace(self.local, request=RequestMapping(stream_usage=True))
        self.assertNotIn("stream_options", profile.build_request([]))
        self.assertTrue(profile.build_request([], stream=True)["stream_options"]["include_usage"])

    def test_deepseek_defaults_to_non_thinking_content(self):
        body = self.deepseek.build_request([], stream=True)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["stream_options"], {"include_usage": True})

    def test_provider_defaults_cannot_override_canonical_fields(self):
        source = (ROOT / "configs/providers/deepseek.toml").read_text()
        source = source.replace(
            'defaults = { thinking = { type = "disabled" } }',
            'defaults = { model = "wrong" }',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.toml"
            path.write_text(source)
            with self.assertRaises(ProfileConfigError):
                load_profile(path, ENV)

    def test_secret_not_in_repr_and_header_built_separately(self):
        self.assertNotIn(ENV["DEEPSEEK_API_KEY"], repr(self.deepseek))
        self.assertEqual(self.deepseek.request_headers()["Authorization"], "Bearer " + ENV["DEEPSEEK_API_KEY"])
        self.assertNotIn("Authorization", self.local.request_headers())

    def test_profiles_have_distinct_cost_status(self):
        usage = TokenUsage(input_tokens=100, output_tokens=10)
        self.assertEqual(self.local.prices.estimate(usage), Decimal(0))
        self.assertIsNone(self.deepseek.prices.estimate(usage))

    def test_price_table_loaded_from_toml_preserves_decimal_and_metadata(self):
        source = (ROOT / "configs/providers/deepseek.toml").read_text()
        source += '\ninput_per_million = "1.25"\noutput_per_million = "3.50"\ncache_read_per_million = "0.25"\ncache_write_per_million = "2.00"\neffective_date = "2026-11-01"\nsource = "Synthetic test prices, not a vendor quote"\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.toml"
            path.write_text(source)
            profile = load_profile(path, ENV)
        self.assertEqual(profile.prices.input_per_million, Decimal("1.25"))
        self.assertEqual(profile.prices.effective_date, "2026-11-01")
        self.assertEqual(profile.prices.estimate(TokenUsage(100, 20, 30, 10)), Decimal("0.0001725"))

    def test_provider_status_override_preserves_default_classifications(self):
        source = (ROOT / "configs/providers/deepseek.toml").read_text()
        source = source.replace('"503" = "transient"', '"503" = "transient"\n"529" = "transient"')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.toml"
            path.write_text(source)
            classifier = load_profile(path, ENV).error_classifier
        self.assertEqual(classifier.classify(status=401).kind, "authentication")
        self.assertEqual(classifier.classify(status=529).kind, "transient")

    def test_deepseek_usage_maps_cache_hits(self):
        usage = self.deepseek.usage_normalizer.normalize({
            "prompt_tokens": 100,
            "completion_tokens": 12,
            "prompt_cache_hit_tokens": 80,
            "prompt_cache_miss_tokens": 20,
        })
        self.assertEqual(usage.input_tokens, 100)
        self.assertEqual(usage.cache_read_tokens, 80)
        self.assertEqual(usage.cache_write_tokens, 0)

    def test_missing_model_or_key_is_rejected(self):
        for missing in ("DEEPSEEK_MODEL", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"):
            with self.subTest(missing=missing), self.assertRaises(ProfileConfigError):
                load_profile(ROOT / "configs/providers/deepseek.toml", {k: v for k, v in ENV.items() if k != missing})

    def test_bad_profile_fails_before_request(self):
        source = (ROOT / "configs/providers/deepseek.toml").read_text()
        mutations = [
            ('reasoning = true', 'reasoning = "true"'),
            ('json_schema = false', 'json_scehma = false'),
            ('reasoning_field = "thinking.type"', 'reasoning_field = ""'),
            ('protocol = "chat_completions"', 'protocol = "native_messages"'),
            ('stream_mode = "cumulative"', 'stream_mode = "guess"'),
            ('currency = "USD"', 'currency = "USD"\ninput_per_million = "-1"'),
            ('currency = "USD"', 'currency = "USD"\ninput_per_million = "NaN"'),
            ('currency = "USD"', 'currency = "USD"\ninput_per_million = "1"'),
            ('currency = "USD"', 'currency = "USD"\ninput_per_million = 1.0'),
            ('"429" = "rate_limit"', '"429" = "typo"'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            for old, new in mutations:
                path.write_text(source.replace(old, new))
                with self.subTest(mutation=old + " -> " + new), self.assertRaises(ProfileConfigError):
                    load_profile(path, ENV)

    def test_env_selection_and_override_without_shell_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            profile_path = ROOT / "configs/providers/local_sglang.toml"
            path.write_text(f'PROVIDER_PROFILE="{profile_path}"\nSGLANG_MODEL=from-file\nSGLANG_BASE_URL=http://localhost:30000/v1\nLITERAL=\'$(echo nope)\'\n')
            env = read_environment(path, {"SGLANG_MODEL": "from-process"})
            self.assertEqual(env["SGLANG_MODEL"], "from-process")
            self.assertEqual(env["LITERAL"], "$(echo nope)")
            self.assertEqual(load_active_profile(directory, {"SGLANG_MODEL": "from-process"}).model, "from-process")

    def test_bad_env_errors_do_not_echo_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            for content in ('TOKEN="SECRET', 'TOKEN=secret value', 'TOKEN=secret\nTOKEN=again'):
                path.write_text(content)
                with self.assertRaises(ProfileConfigError) as ctx:
                    read_environment(path, {})
                self.assertNotIn("secret", str(ctx.exception).lower())

    def test_url_and_header_rejected_without_exposing_secret(self):
        for env in ({**ENV, "DEEPSEEK_BASE_URL": "https://secret@example.invalid"},
                    {**ENV, "DEEPSEEK_API_KEY": "secret\r\nInjected: value"}):
            with self.assertRaises(ProfileConfigError) as ctx:
                load_profile(ROOT / "configs/providers/deepseek.toml", env)
            self.assertNotIn("secret", str(ctx.exception))


class UsageTests(unittest.TestCase):
    def test_cached_and_reasoning_tokens_are_subsets(self):
        normalized = UsageNormalizer().normalize({
            "prompt_tokens": 100, "completion_tokens": 40,
            "prompt_tokens_details": {"cached_tokens": 60},
            "completion_tokens_details": {"reasoning_tokens": 30},
        })
        prices = PriceTable(input_per_million=Decimal("2"), output_per_million=Decimal("8"), cache_read_per_million=Decimal("0.5"))
        self.assertEqual(prices.estimate(normalized), Decimal("0.00043"))

    def test_exclusive_cache_input_mapping(self):
        normalizer = UsageNormalizer(input_tokens="input_tokens", output_tokens="output_tokens",
                                     cache_read_tokens="cache_read_input_tokens", cache_write_tokens="cache_creation_input_tokens",
                                     reasoning_tokens="", input_includes_cache=False)
        usage = normalizer.normalize({"input_tokens": 20, "output_tokens": 10, "cache_read_input_tokens": 60, "cache_creation_input_tokens": 20})
        self.assertEqual(usage.input_tokens, 100)

    def test_flat_cache_fields_can_be_mapped(self):
        normalizer = UsageNormalizer(cache_read_tokens="prompt_cache_hit_tokens")
        self.assertEqual(normalizer.normalize({"prompt_tokens": 100, "completion_tokens": 5, "prompt_cache_hit_tokens": 70}).cache_read_tokens, 70)

    def test_missing_usage_is_unknown_not_zero(self):
        self.assertEqual(UsageNormalizer().normalize(None), TokenUsage())
        self.assertIsNone(PriceTable().estimate(TokenUsage()))
        usage = UsageNormalizer().normalize({"prompt_tokens": 10, "completion_tokens": 5})
        prices = PriceTable(input_per_million=Decimal("1"), output_per_million=Decimal("2"), cache_read_per_million=Decimal("0.1"))
        self.assertIsNone(prices.estimate(usage))

    def test_cumulative_stream_merges_partial_usage_without_double_count(self):
        stream = UsageAccumulator()
        stream.update({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 50}, "completion_tokens": 1})
        stream.update(None)
        stream.update({"completion_tokens": 20})
        stream.update({"completion_tokens": 20})
        usage = stream.normalized(UsageNormalizer())
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.cache_read_tokens), (100, 20, 50))

    def test_delta_stream_is_explicit(self):
        stream = UsageAccumulator("delta")
        stream.update({"prompt_tokens": 100, "completion_tokens": 2})
        stream.update({"completion_tokens": 3})
        self.assertEqual(stream.normalized(UsageNormalizer()).output_tokens, 5)

    def test_exclusive_cache_stream_preserves_initial_input_when_only_output_updates(self):
        normalizer = UsageNormalizer(input_tokens="input_tokens", output_tokens="output_tokens",
                                     cache_read_tokens="cache_read_input_tokens", cache_write_tokens="cache_creation_input_tokens",
                                     input_includes_cache=False)
        stream = UsageAccumulator()
        stream.update({"input_tokens": 20, "cache_read_input_tokens": 60, "cache_creation_input_tokens": 20})
        stream.update({"output_tokens": 10})
        stream.update({"output_tokens": 15})
        usage = stream.normalized(normalizer)
        self.assertEqual((usage.input_tokens, usage.output_tokens), (100, 15))

    def test_invalid_usage_is_rejected(self):
        for value in (-1, True, "100", 2.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                UsageNormalizer().normalize({"prompt_tokens": value})
        with self.assertRaises(ValueError):
            TokenUsage(input_tokens=10, cache_read_tokens=11)
        with self.assertRaises(ValueError):
            TokenUsage(output_tokens=10, reasoning_tokens=11)

    def test_price_table_uses_decimal_and_cache_write_rate(self):
        usage = TokenUsage(100, 20, 30, 10, 5)
        prices = PriceTable(input_per_million=Decimal("1.1"), output_per_million=Decimal("2.2"),
                            cache_read_per_million=Decimal("0.1"), cache_write_per_million=Decimal("1.5"))
        self.assertEqual(prices.estimate(usage), Decimal("0.000128"))


class ErrorTests(unittest.TestCase):
    def test_quota_overrides_retryable_429(self):
        classifier = ErrorClassifier(codes={"insufficient_quota": "quota"})
        error = classifier.classify(status=429, body={"error": {"code": "insufficient_quota"}})
        self.assertEqual(error.kind, "quota")
        self.assertFalse(error.retry_candidate)
        self.assertTrue(classifier.classify(status=429).retry_candidate)

    def test_retry_after_seconds_and_http_date(self):
        classifier = ErrorClassifier()
        self.assertEqual(classifier.classify(status=429, headers={"Retry-After": "2.5"}).retry_after_seconds, 2.5)
        error = classifier.classify(status=503, headers={"retry-after": "Sun, 01 Nov 2026 00:00:10 GMT"}, now=datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(error.retry_after_seconds, 10)
        for value in ("broken", "NaN", "inf"):
            self.assertIsNone(classifier.classify(status=429, headers={"Retry-After": value}).retry_after_seconds)

    def test_cancel_and_permanent_errors_are_not_retry_candidates(self):
        classifier = ErrorClassifier()
        for status in (400, 401, 403, 404, 413, 422):
            self.assertFalse(classifier.classify(status=status).retry_candidate)
        self.assertFalse(classifier.classify(transport_error="cancelled").retry_candidate)
        self.assertEqual(classifier.classify(transport_error="timeout").kind, "timeout")
        self.assertFalse(classifier.classify().retry_candidate)

    def test_stream_error_inside_http_200_uses_body_code(self):
        classifier = ErrorClassifier(code_paths=("error.type",), codes={"overloaded_error": "transient"})
        self.assertTrue(classifier.classify(status=200, body={"error": {"type": "overloaded_error"}}).retry_candidate)


if __name__ == "__main__":
    unittest.main()
