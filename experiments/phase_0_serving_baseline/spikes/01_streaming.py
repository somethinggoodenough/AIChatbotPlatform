#!/usr/bin/env python3
"""Verify incremental output AND final usage on both configured backends."""

from _common import check_completed_stream, collect_stream, parser, positive_int, require, run


def probe(profile, args, evidence):
    body = profile.build_request(
        [{"role": "user", "content": "Write a numbered list of twenty short facts about the ocean. Use one sentence per item."}],
        stream=True, max_output_tokens=args.max_output_tokens,
    )
    require(body.get("stream_options") == {"include_usage": True}, "Profile must enable request.stream_usage")
    evidence["request"] = body
    evidence.update(collect_stream(profile, body, args.timeout))
    check_completed_stream(evidence)


if __name__ == "__main__":
    cli = parser(__doc__, both=True)
    cli.add_argument("--max-output-tokens", type=positive_int, default=256)
    raise SystemExit(run("01_streaming", cli.parse_args(), probe))
