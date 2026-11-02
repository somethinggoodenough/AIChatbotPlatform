#!/usr/bin/env python3
"""Close a live stream and observe scheduler gauges on a dedicated SGLang server."""

import time
from _common import (
    Inconclusive, ProbeFailure, check_completed_stream, collect_stream, parser,
    positive, positive_int, post_json, require, run, running_snapshot, stream_events,
)


def wait_idle(profile, args, evidence, key):
    start = time.monotonic()
    zero_since = None
    samples = evidence.setdefault(key, [])
    while time.monotonic() - start < args.abort_deadline:
        snapshot = running_snapshot(profile, min(args.timeout, args.abort_deadline))
        now = time.monotonic()
        samples.append({"seconds": now - start, **snapshot})
        if snapshot["running"] == 0 and snapshot["queued"] == 0:
            zero_since = now if zero_since is None else zero_since
            if now - zero_since >= args.stable_window and now - start <= args.abort_deadline:
                return now - start
        else:
            zero_since = None
        time.sleep(args.poll_interval)
    raise ProbeFailure("Server did not return to stable running=0, queued=0 within deadline")


def probe(profile, args, evidence):
    evidence["settings"] = {k: getattr(args, k) for k in (
        "abort_deadline", "stable_window", "poll_interval", "max_output_tokens", "close_after_chunks",
    )}
    # A tiny completed request initializes scheduler metrics; it does not flush cache.
    post_json(profile, profile.build_request([{"role": "user", "content": "Say hi."}], max_output_tokens=2), args.timeout)
    baseline = running_snapshot(profile, min(args.timeout, args.abort_deadline))
    evidence["initial_snapshot"] = baseline
    if baseline["running"] or baseline["queued"]:
        raise Inconclusive("Server is busy; run this probe alone on a dedicated instance")
    wait_idle(profile, args, evidence, "baseline")
    body = profile.build_request([
        {"role": "user", "content": "Continue writing a numbered list of short facts about nature. Keep producing new items."},
    ], stream=True, max_output_tokens=args.max_output_tokens, temperature=0)
    # SGLang-specific experimental control. Never applied to a hosted API profile.
    body["ignore_eos"] = True
    body["min_tokens"] = args.max_output_tokens
    evidence["request"] = body
    control = collect_stream(profile, body, args.timeout)
    evidence["uninterrupted_control"] = control
    check_completed_stream(control)
    require(control["finish_reasons"] == ["length"], "Control did not run to token limit")
    require(control["normalized_usage"]["output_tokens"] == args.max_output_tokens, "Control token count differs from requested limit")
    if control["decode_seconds"] <= 2 * (args.abort_deadline + args.stable_window):
        raise Inconclusive("Natural generation is too fast to distinguish cancellation; increase max-output-tokens AND server context-length, or shorten the deadline")
    wait_idle(profile, args, evidence, "after_control")

    events = stream_events(profile, body, args.timeout)
    started = time.monotonic()
    content_chunks = 0
    observed_running = False
    try:
        for event in events:
            if event is None:
                raise Inconclusive("Request finished before client disconnect")
            evidence["request_id"] = event.get("id", evidence.get("request_id"))
            for choice in event.get("choices", []):
                if choice.get("finish_reason"):
                    raise Inconclusive("Request finished before running-state observation")
                if (choice.get("delta") or {}).get("content"):
                    content_chunks += 1
            if content_chunks >= args.close_after_chunks:
                snapshot = running_snapshot(profile, min(args.timeout, args.abort_deadline))
                evidence.setdefault("before_disconnect", []).append(snapshot)
                if snapshot["running"] > 0 and snapshot["queued"] == 0:
                    observed_running = True
                    break
    finally:
        # This closes the actual HTTP connection, not just the iterator consumer.
        events.close()
    evidence["chunks_before_disconnect"] = content_chunks
    evidence["seconds_before_disconnect"] = time.monotonic() - started
    require(observed_running, "Never observed an active server request before closing the client")
    elapsed = wait_idle(profile, args, evidence, "after_disconnect")
    evidence["disconnect_to_stable_idle_seconds"] = elapsed
    if evidence["seconds_before_disconnect"] + elapsed >= control["decode_seconds"] / 2:
        raise Inconclusive("Observation too close to natural completion time; cannot attribute disappearance to disconnect")
    evidence["server_request_disappeared"] = True
    evidence["scope"] = "Dedicated single-model server; running and queue gauges, calibrated against an uninterrupted same-length control. No explicit abort API call."


if __name__ == "__main__":
    cli = parser(__doc__)
    cli.add_argument("--max-output-tokens", type=positive_int, default=3072)
    cli.add_argument("--close-after-chunks", type=positive_int, default=4)
    cli.add_argument("--abort-deadline", type=positive, default=5)
    cli.add_argument("--stable-window", type=positive, default=1)
    cli.add_argument("--poll-interval", type=positive, default=0.2)
    args = cli.parse_args()
    if args.stable_window >= args.abort_deadline or args.poll_interval >= args.abort_deadline:
        cli.error("stable-window and poll-interval must be shorter than abort-deadline")
    raise SystemExit(run("04_disconnect", args, probe))
