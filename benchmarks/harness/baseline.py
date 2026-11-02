"""Reproduce the serving baseline: JSON measurements and an SVG plot.

Run: uv run python -m benchmarks.harness.baseline
Default is offline mock-only. --local explicitly selects configured SGLang.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
import math
from pathlib import Path
import random
import time
from uuid import uuid4

from ai_serving.backends.providers.backend import ModelCompatBackend, SGLangBackend
from ai_serving.config.providers import load_profile, read_environment
from ai_serving.contracts import ChatRequest
from ai_serving.testing import MockConfig, MockServer


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


@asynccontextmanager
async def case_backend(backend, config):
    if backend is not None:
        yield backend, None
        return
    async with MockServer(config) as server:
        mock_backend = ModelCompatBackend(server.profile())
        try:
            yield mock_backend, server
        finally:
            await mock_backend.aclose()


async def run_case(concurrency: int, count: int, workload: str, seed: int,
                   input_lengths: tuple[int, ...], output_lengths: tuple[int, ...],
                   backend: ModelCompatBackend | None = None) -> dict:
    rng = random.Random(seed)
    # Mock instances are already isolated. Real servers keep their cache between cases.
    namespace = "" if backend is None else f"benchmark-case:{uuid4().hex}\n"
    config = MockConfig(ttft=0.003, decode_delay=0.0005, prefill_per_token=0.00001,
                        response_text="token " * max(output_lengths))
    async with case_backend(backend, config) as (client, server):
        semaphore = asyncio.Semaphore(concurrency)
        measurements = []
        started = time.perf_counter()

        async def request(index: int, prompt_length: int, output_length: int) -> None:
            prefix = namespace + ("Stable instruction. " * 64 if workload != "chat" else f"Namespace {index}. ")
            response_format = None
            if workload == "judgment":
                response_format = {"type": "json_schema", "json_schema": {"name": "router_decision",
                    "schema": {"type": "object", "properties": {"action": {"enum": ["reply", "spawn", "ignore"]}},
                               "required": ["action"]}}}
                output_length = max(output_length, 16)
            req = ChatRequest(messages=[{"role": "system", "content": prefix},
                {"role": "user", "content": "x" * (prompt_length * 4)}], model=client.profile.model,
                max_tokens=output_length, response_format=response_format)
            async with semaphore:
                begin = time.perf_counter()
                first = previous = None
                intervals = []
                usage = None
                text = ""
                async for chunk in client.stream_chat(req):
                    now = time.perf_counter()
                    if chunk.text:
                        text += chunk.text
                        if first is None:
                            first = now
                        if previous is not None:
                            intervals.append((now - previous) * 1000)
                        previous = now
                    if chunk.usage is not None:
                        usage = chunk.usage
                if workload == "judgment":
                    json.loads(text)
                measurements.append({"ttft_ms": ((first or time.perf_counter()) - begin) * 1000,
                    "itl_ms": intervals, "latency_ms": (time.perf_counter() - begin) * 1000,
                    "usage": usage})

        async with asyncio.TaskGroup() as group:
            for i in range(count):
                group.create_task(request(i, rng.choice(input_lengths), rng.choice(output_lengths)))
        elapsed = time.perf_counter() - started
        ttfts = [row["ttft_ms"] for row in measurements]
        intervals = [value for row in measurements for value in row["itl_ms"]]
        latencies = [row["latency_ms"] for row in measurements]
        def usage_total(key):
            values = [(row["usage"] or {}).get(key) for row in measurements]
            return None if None in values else sum(values)

        input_tokens = usage_total("prompt_tokens")
        output_tokens = usage_total("completion_tokens")
        cached_tokens = usage_total("cached_tokens")
        return {"workload": workload, "concurrency": concurrency, "requests": count,
                "model": client.profile.model,
                "ttft_p50_ms": percentile(ttfts, 0.5), "ttft_p99_ms": percentile(ttfts, 0.99),
                "itl_p50_ms": percentile(intervals, 0.5), "itl_p99_ms": percentile(intervals, 0.99),
                "latency_p50_ms": percentile(latencies, 0.5), "latency_p99_ms": percentile(latencies, 0.99),
                "output_tokens_per_second": output_tokens / elapsed if output_tokens is not None else None,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens, "cache_read_tokens": cached_tokens,
                "cache_read_ratio": cached_tokens / input_tokens if input_tokens and cached_tokens is not None else None,
                "cost_usd": 0, "server": asdict(server.stats) if server else None, "samples": measurements}


def plot_svg(rows: list[dict], mode: str = "mock-only") -> str:
    width, height = 920, 70 + 42 * len(rows)
    maximum = max((row["ttft_p99_ms"] for row in rows), default=1)
    pieces = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" role="img" '
              'aria-label="Baseline TTFT p50 and p99">', '<rect width="100%" height="100%" fill="white"/>',
              f'<g font-family="sans-serif" font-size="13"><text x="20" y="25">{mode} TTFT: blue p50, gray p99 (milliseconds)</text>']
    for index, row in enumerate(rows):
        y = 48 + index * 42
        pieces.append(f'<text x="20" y="{y + 16}">{row["workload"]} / c={row["concurrency"]}</text>')
        for key, color, offset in (("ttft_p99_ms", "#cbd5e1", 0), ("ttft_p50_ms", "#2563eb", 14)):
            length = row[key] / maximum * 550
            pieces.append(f'<rect x="230" y="{y + offset}" width="{length:.1f}" height="11" fill="{color}"/>')
        pieces.append(f'<text x="795" y="{y + 16}">{row["ttft_p50_ms"]:.2f} / {row["ttft_p99_ms"]:.2f}</text>')
    return "\n".join(pieces + ["</g></svg>"])


async def benchmark(args: argparse.Namespace) -> dict:
    rows = []
    backend = None
    if args.local:
        root = Path(__file__).resolve().parents[2]
        profile = load_profile(root / "configs/providers/local_sglang.toml", read_environment(root / ".env"))
        backend = SGLangBackend(profile)
    try:
        for workload in args.workloads:
            for concurrency in args.concurrency:
                rows.append(await run_case(concurrency, args.requests, workload, args.seed,
                                           tuple(args.input_lengths), tuple(args.output_lengths), backend))
    finally:
        if backend:
            await backend.aclose()
    mode = "local-sglang" if args.local else "mock-only"
    limitations = ["ITL fields measure inter-chunk gaps; real chunks may contain multiple model tokens.",
                  "Input lengths control four-character units; real token counts come from returned usage.",
                  "Zero API fee excludes GPU, electricity and hosting costs."]
    if args.local:
        limitations.append("Each case has a unique fixed-length prefix namespace; no cache flush is performed.")
    else:
        limitations.extend(["Synthetic four-character tokens; not GPU performance.",
                            "No paid API, Discord or SGLang inference was exercised."])
    result = {"mode": mode, "seed": args.seed, "input_lengths": args.input_lengths,
              "output_lengths": args.output_lengths, "results": rows,
              "limitations": limitations}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "baseline.json").write_text(json.dumps(result, indent=2) + "\n")
    (args.output / "baseline.svg").write_text(plot_svg(rows, mode))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", help="Use only the configured local_sglang profile; no paid API fallback")
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 4, 8])
    parser.add_argument("--requests", type=int, default=24)
    parser.add_argument("--input-lengths", nargs="+", type=int, default=[16, 64, 256])
    parser.add_argument("--output-lengths", nargs="+", type=int, default=[8, 32])
    parser.add_argument("--workloads", nargs="+", choices=["chat", "shared_prefix", "judgment"],
                        default=["chat", "shared_prefix", "judgment"])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.concurrency + args.input_lengths + args.output_lengths + [args.requests]) < 1:
        parser.error("Counts, concurrency and lengths must be positive")
    if args.output is None:
        args.output = Path("artifacts/benchmarks/sglang" if args.local else "artifacts/benchmarks/phase0")
    result = asyncio.run(benchmark(args))
    print(f'{result["mode"]}: workload concurrency TTFT-p50-ms TTFT-p99-ms tokens/s cache-read-ratio')
    for row in result["results"]:
        throughput = "unknown" if row["output_tokens_per_second"] is None else f'{row["output_tokens_per_second"]:.1f}'
        cached = "unknown" if row["cache_read_ratio"] is None else f'{row["cache_read_ratio"]:.3f}'
        print(f'{row["workload"]} {row["concurrency"]} {row["ttft_p50_ms"]:.2f} '
              f'{row["ttft_p99_ms"]:.2f} {throughput} {cached}')
    print(f"Results: {args.output / 'baseline.json'} and baseline.svg")


if __name__ == "__main__":
    main()
