"""Offline routing comparison. All numbers describe simulators, not SGLang."""

import argparse
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
from time import perf_counter

from ai_serving.backends.providers import ModelCompatBackend
from ai_serving.contracts import ChatRequest
from ai_serving.contracts.messages import Message
from ai_serving.events.batching import EventBatcher
from ai_serving.gateway.routing import BackendPolicy, RetryContext, Route, RoutedGateway, prefix_key
from ai_serving.observability.metrics import percentile
from ai_serving.testing.mock_server import MockConfig, MockFault, MockServer


POLICY = BackendPolicy(first_timeout=0.03, idle_timeout=0.03, max_retries=0,
                       failure_threshold=1, reset_after=1)
CONTEXT = RetryContext(retry_safe=True)


async def stack_for(stack: AsyncExitStack, names: tuple[str, ...]):
    servers, backends = {}, {}
    for name in names:
        servers[name] = await stack.enter_async_context(MockServer(MockConfig(
            ttft=0.002, decode_delay=0.0005, response_text=name, prefill_per_token=0.00003,
        )))
        profile = servers[name].profile(name)
        backends[name] = ModelCompatBackend(profile)
    return servers, backends


async def outage_case(fault: str, requests: int, concurrency: int) -> dict:
    async with AsyncExitStack() as stack:
        servers, backends = await stack_for(stack, ("primary", "secondary", "local"))
        gateway = RoutedGateway(backends, {"default": Route(("primary",), ("secondary", "local"))}, policy=POLICY)
        stack.push_async_callback(gateway.aclose)
        if fault == "killed_replica":
            await servers["primary"].aclose()
        elif fault == "timeout":
            servers["primary"].config.fault = MockFault(hang=True)
        elif fault == "drop_before_output":
            servers["primary"].config.fault = MockFault(disconnect_after=0)
        elif fault == "drop_midstream":
            servers["primary"].config.fault = MockFault(disconnect_after=1)
        elif fault != "healthy":
            servers["primary"].config.fault = MockFault(status=int(fault))
        slots = asyncio.Semaphore(concurrency)

        async def one():
            async with slots:
                start = perf_counter()
                first, text = None, ""
                try:
                    async for chunk in gateway.stream_chat(ChatRequest([], "configured"), CONTEXT):
                        if chunk.text:
                            first = first or perf_counter()
                            text += chunk.text
                    failed = False
                except Exception:
                    failed = True
                return {"latency_ms": (perf_counter() - start) * 1000,
                        "ttft_ms": ((first or perf_counter()) - start) * 1000,
                        "failed": failed, "fallback": not failed and text != "primary"}

        rows = await asyncio.gather(*(one() for _ in range(requests)))
        return {"scenario": fault, "requests": requests, "concurrency": concurrency,
                "latency_p99_ms": percentile([r["latency_ms"] for r in rows], 0.99),
                "ttft_p99_ms": percentile([r["ttft_ms"] for r in rows], 0.99),
                "fallback_rate": sum(r["fallback"] for r in rows) / requests,
                "failure_rate": sum(r["failed"] for r in rows) / requests,
                "costs": gateway.ledger.totals()}


async def locality_case(prefix_aware: bool, count: int) -> dict:
    async with AsyncExitStack() as stack:
        servers, backends = await stack_for(stack, ("a", "b"))
        gateway = RoutedGateway(backends, {"default": Route(("a", "b"))},
                                policy=POLICY, prefix_aware=prefix_aware)
        stack.push_async_callback(gateway.aclose)
        ttft, prompt, cached = [], 0, 0
        for index in range(count):
            req = ChatRequest([{"role": "system", "content": f"namespace-{index // 2} " + "fixed " * 100},
                               {"role": "user", "content": f"question {index}"}], "configured")
            start, first = perf_counter(), None
            async for chunk in gateway.stream_chat(req, CONTEXT):
                if chunk.text and first is None:
                    first = perf_counter()
                if chunk.usage:
                    prompt += chunk.usage["prompt_tokens"]
                    cached += chunk.usage["cached_tokens"]
            ttft.append(((first or perf_counter()) - start) * 1000)
        return {"policy": "prefix_aware" if prefix_aware else "round_robin_mock_baseline",
                "cache_read_ratio": cached / prompt, "ttft_p50_ms": percentile(ttft, .5),
                "ttft_p99_ms": percentile(ttft, .99)}


async def resume_case(prefix_aware: bool) -> dict:
    async with AsyncExitStack() as stack:
        servers, backends = await stack_for(stack, ("lost", "cold", "warm"))
        gateway = RoutedGateway(backends, {"default": Route(("lost", "cold", "warm"))},
                                policy=POLICY, prefix_aware=prefix_aware)
        stack.push_async_callback(gateway.aclose)
        req = ChatRequest([{"role": "system", "content": "shared task context " * 100},
                           {"role": "user", "content": "resume"}], "configured")
        for name in ("lost", "warm"):
            await backends[name].chat(req)
            gateway.prefixes[name].add(prefix_key(req))
        await servers["lost"].aclose()
        result = await gateway.chat(req, CONTEXT)
        return {"prefix_aware": prefix_aware, "selected": result.text,
                "reprefill_tokens": result.usage["prompt_tokens"] - result.usage["cached_tokens"],
                "cached_tokens": result.usage["cached_tokens"]}


async def batching_case(window: float, count: int) -> dict:
    async with AsyncExitStack() as stack:
        servers, backends = await stack_for(stack, ("judge",))
        gateway = RoutedGateway(backends, {"default": Route(("judge",))}, policy=POLICY)
        stack.push_async_callback(gateway.aclose)
        latencies = []
        schema = {"type": "json_schema", "json_schema": {"name": "decision", "schema": {
            "type": "object", "properties": {"action": {"enum": ["reply"]}}, "required": ["action"]}}}

        async def decide(messages):
            start = perf_counter()
            req = ChatRequest([{"role": "system", "content": "Decide once for the following arrivals."},
                               {"role": "user", "content": "\n".join(item.text for item in messages)}],
                              "configured", response_format=schema)
            result = await gateway.chat(req, CONTEXT)
            json.loads(result.text)
            latencies.append((perf_counter() - start) * 1000)

        batcher = EventBatcher(decide, window)
        stack.push_async_callback(batcher.aclose)
        started = perf_counter()
        await asyncio.gather(*(batcher.submit(Message(str(i), "user", "channel", f"arrival {i}"))
                               for i in range(count)))
        return {"window_seconds": window, "arrivals": count, "router_decisions": len(latencies),
                "decision_latency_p50_ms": percentile(latencies, .5),
                "decision_latency_p99_ms": percentile(latencies, .99),
                "all_arrivals_completed_ms": (perf_counter() - started) * 1000}


async def benchmark(requests: int = 24, concurrency: int = 6) -> dict:
    faults = ["healthy", "503", "429", "timeout", "killed_replica", "drop_before_output", "drop_midstream"]
    outages = [await outage_case(fault, requests, concurrency) for fault in faults]
    baseline = outages[0]
    for row in outages:
        row["latency_p99_delta_ms"] = row["latency_p99_ms"] - baseline["latency_p99_ms"]
        row["ttft_p99_delta_ms"] = row["ttft_p99_ms"] - baseline["ttft_p99_ms"]
    return {"mode": "mock-only", "outages": outages,
            "prefix_routing": [await locality_case(value, requests) for value in (False, True)],
            "resume": [await resume_case(value) for value in (False, True)],
            "batching": [await batching_case(window, requests) for window in (0, .01)],
            "limitations": ["Round-robin simulator comparison is not SGLang Model Gateway cache_aware.",
                            "Midstream failures are deliberately surfaced; no duplicate text is appended.",
                            "Mock token counts and zero API fees do not predict GPU/provider performance.",
                            "Retry-safe generation is explicitly enabled for synthetic workloads."]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=24)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--output", type=Path, default=Path("artifacts/benchmarks/phase3/routing.json"))
    args = parser.parse_args()
    if args.requests < 2 or args.concurrency < 1:
        parser.error("At least two requests and positive concurrency are required")
    result = asyncio.run(benchmark(args.requests, args.concurrency))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Mock routing results: {args.output}")
    for row in result["outages"]:
        print(f'{row["scenario"]}: p99 delta {row["latency_p99_delta_ms"]:.2f} ms, '
              f'fallback {row["fallback_rate"]:.1%}, failure {row["failure_rate"]:.1%}')


if __name__ == "__main__":
    main()
