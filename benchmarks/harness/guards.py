"""Offline guard latency and fixture scoring. This is not a safety evaluation."""

import argparse
import asyncio
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from time import perf_counter

from ai_serving.backends.providers import ModelCompatBackend
from ai_serving.contracts import ChatRequest
from ai_serving.gateway import Assignment, Gateway
from ai_serving.guards.normalization import looks_risky
from ai_serving.guards.pipeline import GuardedGateway, GuardPolicy, ModelChecks
from ai_serving.observability.metrics import percentile
from ai_serving.testing import MockConfig, MockServer


def fixture_classifier(body):
    text = body["messages"][-1]["content"]
    name = body.get("response_format", {}).get("json_schema", {}).get("name")
    if name == "fixture_judge":
        data = json.loads(text)
        return {"pass": data["expected_reject"] == data["observed_reject"]}
    return {"allow": not looks_risky(text)}


async def run(output: Path, samples: int = 20):
    cases = json.loads(Path("configs/workloads/red_team.json").read_text())
    async with MockServer(MockConfig(response_text="A harmless mock answer. " * 16)) as server, \
            MockServer(MockConfig(ttft=0.001, responder=fixture_classifier)) as classifier:
        raw = Gateway({"main": ModelCompatBackend(server.profile("main")),
                       "classifier": ModelCompatBackend(classifier.profile("classifier"))},
                      {"default": Assignment("main"), "input_classifier": Assignment("classifier"),
                       "topic_classifier": Assignment("classifier"), "judge": Assignment("classifier")})
        guard = GuardedGateway(raw, ModelChecks(raw), policy=GuardPolicy(max_session_turns=1000),
                               cost_reader=lambda request_id: Decimal(0))
        timings = {"baseline": [], "guarded": []}
        scores = {}
        try:
            request = ChatRequest([{"role": "user", "content": "hello"}], "configured")
            for label, gateway in (("baseline", raw), ("guarded", guard)):
                for _ in range(samples):
                    start = perf_counter()
                    first = None
                    async for chunk in gateway.stream_chat(request):
                        if chunk.text and first is None:
                            first = perf_counter() - start
                    timings[label].append(first * 1000)
            judge_format = {"type": "json_schema", "json_schema": {"name": "fixture_judge", "schema": {
                "type": "object", "properties": {"pass": {"type": "boolean"}}, "required": ["pass"]}}}
            for case in cases:
                server.config.response_text = ("PRIVATE_SYSTEM_PROMPT" if case.get("leak") else "A harmless answer.")
                messages = [{"role": "user", "content": text} for text in case.get("history", [])]
                messages.append({"role": "user", "content": case["text"]})
                answer = await guard.chat(replace(request, messages=messages))
                observed = answer.finish_reason == "content_filter"
                judge = await raw.chat(ChatRequest([
                    {"role": "system", "content": "Score whether the observed refusal matches the labeled fixture."},
                    {"role": "user", "content": json.dumps({"expected_reject": case["reject"], "observed_reject": observed})},
                ], "configured", call_type="judge", response_format=judge_format))
                category = scores.setdefault(case["category"], {"passed": 0, "total": 0})
                category["total"] += 1
                category["passed"] += int(json.loads(judge.text)["pass"])
            latency = {label: {"p50_ms": percentile(values, .5), "p99_ms": percentile(values, .99)}
                       for label, values in timings.items()}
            result = {"mode": "mock-only", "samples": samples, "ttft": latency,
                      "overhead_ms": {q: latency["guarded"][q] - latency["baseline"][q] for q in ("p50_ms", "p99_ms")},
                      "fixture_scores": scores, "judge_calibration": "SKIP: no real model or independent human calibration",
                      "limitations": "Scripted classifiers/judge test wiring only. No claim of public-server safety."}
            output.mkdir(parents=True, exist_ok=True)
            (output / "guards.json").write_text(json.dumps(result, indent=2) + "\n")
            return result
        finally:
            await guard.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--output", type=Path, default=Path("artifacts/benchmarks/phase4"))
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.output, args.samples)), indent=2))
