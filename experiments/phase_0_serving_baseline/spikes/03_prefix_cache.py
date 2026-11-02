#!/usr/bin/env python3
"""Compare a run-unique cold prefix against sequential shared-prefix requests."""

import uuid
from _common import parser, positive_int, post_json, require, run


def probe(profile, args, evidence):
    marker = uuid.uuid4().hex
    # Unique marker comes FIRST: no flush_cache mutation or prior-run warm-prefix pollution.
    system = f"Experiment namespace {marker}.\n" + "\n".join(
        f"Reference rule {i}: answer briefly, preserve facts, and use the shared handbook as context."
        for i in range(args.prefix_lines)
    )
    evidence.update(prefix_lines=args.prefix_lines, namespace=marker, trials=[])
    for index in range(args.requests):
        body = profile.build_request([
            {"role": "system", "content": system},
            {"role": "user", "content": f"Request {index}: acknowledge the handbook in one short sentence."},
        ], max_output_tokens=24)
        response = post_json(profile, body, args.timeout)
        usage = response.get("usage") or {}
        # In some builds zero cache hits omit prompt_tokens_details. Cold is still reported as unknown.
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        row = {"index": index, "usage": usage, "cached_tokens": cached, "request_id": response.get("id")}
        evidence["trials"].append(row)
        require(bool(response.get("choices")), "No completion choices")
        require(type(usage.get("prompt_tokens")) is int and usage["prompt_tokens"] >= args.min_prefix_tokens,
                "Prompt too short for the intended cache experiment")
    cold, *warm = evidence["trials"]
    # A hot run must show substantial reuse, not just chat-template BOS tokens.
    for row in warm:
        cached = row["cached_tokens"]
        require(type(cached) is int and 0 <= cached <= row["usage"]["prompt_tokens"], "Missing/invalid cached_tokens; enable --enable-cache-report")
        require(cached >= args.min_prefix_tokens, "Shared long prefix did not produce substantial cache reuse")
        require(cached / row["usage"]["prompt_tokens"] >= 0.5, "Less than half of prompt came from cache")
        if cold["cached_tokens"] is not None:
            require(cached > cold["cached_tokens"], "Warm request did not improve on the run-unique cold request")
    evidence["cold_missing_cache_field"] = cold["cached_tokens"] is None
    evidence["log_cross_check"] = "Correlate this isolated run with SGLang prefill #cached-token logs; usage is the machine-checked evidence."


if __name__ == "__main__":
    cli = parser(__doc__)
    cli.add_argument("--prefix-lines", type=positive_int, default=64)
    cli.add_argument("--requests", type=positive_int, default=4)
    cli.add_argument("--min-prefix-tokens", type=positive_int, default=512)
    args = cli.parse_args()
    if args.requests < 2:
        cli.error("--requests must be at least 2")
    raise SystemExit(run("03_prefix_cache", args, probe))
