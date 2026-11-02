"""Reproduce the offline serving baseline: JSON measurements and an SVG plot.

Run: uv run python -m benchmarks.harness.baseline
No .env, external endpoint, GPU or paid API is used.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import math
from pathlib import Path
import random
import time

from ai_serving.backends.providers.backend import ModelCompatBackend
from ai_serving.contracts import ChatRequest
from ai_serving.testing import MockConfig, MockServer


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


async def run_case(concurrency: int, count: int, workload: str, seed: int,
                   input_lengths: tuple[int, ...], output_lengths: tuple[int, ...]) -> dict:
    rng = random.Random(seed)
    config = MockConfig(ttft=0.003, decode_delay=0.0005, prefill_per_token=0.00001,
                        response_text="token " * max(output_lengths))
    async with MockServer(config) as server:
        backend = ModelCompatBackend(server.profile())
        semaphore = asyncio.Semaphore(concurrency)
        measurements = []
        started = time.perf_counter()

        async def request(index: int, prompt_length: int, output_length: int) -> None:
            prefix = "Stable instruction. " * 64 if workload != "chat" else f"Namespace {index}. "
            response_format = None
            if workload == "judgment":
                response_format = {"type": "json_schema", "json_schema": {"name": "router_decision",
                    "schema": {"type": "object", "properties": {"action": {"enum": ["reply", "spawn", "ignore"]}},
                               "required": ["action"]}}}
                output_length = max(output_length, 16)
            req = ChatRequest(messages=[{"role": "system", "content": prefix},
                {"role": "user", "content": "x" * (prompt_length * 4)}], model="mock-model",
                max_tokens=output_length, response_format=response_format)
            async with semaphore:
                begin = time.perf_counter()
                first = previous = None
                intervals = []
                usage = None
                text = ""
                async for chunk in backend.stream_chat(req):
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

        try:
            await asyncio.gather(*(request(i, rng.choice(input_lengths), rng.choice(output_lengths))
                                   for i in range(count)))
        finally:
            await backend.aclose()
        elapsed = time.perf_counter() - started
        ttfts = [row["ttft_ms"] for row in measurements]
        intervals = [value for row in measurements for value in row["itl_ms"]]
        latencies = [row["latency_ms"] for row in measurements]
        input_tokens = sum(row["usage"]["prompt_tokens"] for row in measurements)
        output_tokens = sum(row["usage"]["completion_tokens"] for row in measurements)
        cached_tokens = sum(row["usage"]["cached_tokens"] for row in measurements)
        return {"workload": workload, "concurrency": concurrency, "requests": count,
                "ttft_p50_ms": percentile(ttfts, 0.5), "ttft_p99_ms": percentile(ttfts, 0.99),
                "itl_p50_ms": percentile(intervals, 0.5), "itl_p99_ms": percentile(intervals, 0.99),
                "latency_p50_ms": percentile(latencies, 0.5), "latency_p99_ms": percentile(latencies, 0.99),
                "output_tokens_per_second": output_tokens / elapsed, "input_tokens": input_tokens,
                "output_tokens": output_tokens, "cache_read_tokens": cached_tokens,
                "cache_read_ratio": cached_tokens / input_tokens if input_tokens else 0,
                "cost_usd": 0, "server": asdict(server.stats), "samples": measurements}


def plot_svg(rows: list[dict]) -> str:
    width, height = 920, 70 + 42 * len(rows)
    maximum = max((row["ttft_p99_ms"] for row in rows), default=1)
    pieces = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" role="img" '
              'aria-label="Mock baseline TTFT p50 and p99">', '<rect width="100%" height="100%" fill="white"/>',
              '<g font-family="sans-serif" font-size="13"><text x="20" y="25">Mock TTFT: blue p50, gray p99 (milliseconds)</text>']
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
    for workload in args.workloads:
        for concurrency in args.concurrency:
            rows.append(await run_case(concurrency, args.requests, workload, args.seed,
                                       tuple(args.input_lengths), tuple(args.output_lengths)))
    result = {"mode": "mock-only", "seed": args.seed, "input_lengths": args.input_lengths,
              "output_lengths": args.output_lengths, "results": rows,
              "limitations": ["Synthetic 4-character tokens; not a tokenizer or GPU performance result.",
                              "ITL measures emitted chunks (one synthetic token), not real provider tokens.",
                              "No paid API, Discord or SGLang inference was exercised."]}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "baseline.json").write_text(json.dumps(result, indent=2) + "\n")
    (args.output / "baseline.svg").write_text(plot_svg(rows))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 4, 8])
    parser.add_argument("--requests", type=int, default=24)
    parser.add_argument("--input-lengths", nargs="+", type=int, default=[16, 64, 256])
    parser.add_argument("--output-lengths", nargs="+", type=int, default=[8, 32])
    parser.add_argument("--workloads", nargs="+", choices=["chat", "shared_prefix", "judgment"],
                        default=["chat", "shared_prefix", "judgment"])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, default=Path("artifacts/benchmarks/phase0"))
    args = parser.parse_args()
    if min(args.concurrency + args.input_lengths + args.output_lengths + [args.requests]) < 1:
        parser.error("Counts, concurrency and lengths must be positive")
    result = asyncio.run(benchmark(args))
    print("Mock only: workload concurrency TTFT-p50-ms TTFT-p99-ms tokens/s cache-read-ratio")
    for row in result["results"]:
        print(f'{row["workload"]} {row["concurrency"]} {row["ttft_p50_ms"]:.2f} '
              f'{row["ttft_p99_ms"]:.2f} {row["output_tokens_per_second"]:.1f} {row["cache_read_ratio"]:.3f}')
    print(f"Results: {args.output / 'baseline.json'} and baseline.svg")


if __name__ == "__main__":
    main()
