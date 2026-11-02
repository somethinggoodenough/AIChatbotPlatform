#!/usr/bin/env python3
"""Send a schema and parse unmodified content, in non-streaming and streaming modes."""

import json
from _common import check_completed_stream, collect_stream, parser, post_json, require, run


SCHEMA = {
    "type": "object", "properties": {
        "action": {"type": "string", "enum": ["reply", "spawn", "ignore"]},
        "reason": {"type": "string"},
        "priority": {"type": "integer", "minimum": 0, "maximum": 2},
    }, "required": ["action", "reason", "priority"], "additionalProperties": False,
}


def parse_decision(content):
    # Validate this fixed spike schema explicitly; not a general JSON Schema engine.
    # No markdown stripping, JSON repair, regex extraction or second model call.
    result = json.loads(content, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-JSON number")))
    require(isinstance(result, dict) and set(result) == {"action", "reason", "priority"}, "Wrong schema properties")
    require(result["action"] in ("reply", "spawn", "ignore"), "Invalid action enum")
    require(isinstance(result["reason"], str), "reason must be string")
    require(type(result["priority"]) is int and 0 <= result["priority"] <= 2, "Invalid priority")
    return result


def probe(profile, args, evidence):
    evidence.update(schema=SCHEMA, trials=[])
    for stream in (False, True):
        body = profile.build_request([
            {"role": "system", "content": "Classify the message. reply for greetings, spawn for tasks needing tools, ignore for bot noise. Return JSON matching the schema."},
            {"role": "user", "content": "Hello! How are you today?"},
        ], stream=stream, max_output_tokens=256, json_schema=SCHEMA, schema_name="router_decision")
        row = {"stream": stream, "request": body}
        evidence["trials"].append(row)
        if stream:
            require(body.get("stream_options") == {"include_usage": True}, "Streaming schema probe requires usage")
            row.update(collect_stream(profile, body, args.timeout))
            check_completed_stream(row, min_chunks=1)
            require(row["finish_reasons"] == ["stop"], "Structured output truncated or did not stop normally")
            content = row["content"]
        else:
            response = post_json(profile, body, args.timeout)
            row["response"] = response
            choice = response["choices"][0]
            require(choice["finish_reason"] == "stop", "Structured output truncated or refused")
            require(not choice["message"].get("refusal"), "Structured request refused")
            content = choice["message"]["content"]
        row["parsed"] = parse_decision(content)


if __name__ == "__main__":
    raise SystemExit(run("02_structured_output", parser(__doc__).parse_args(), probe))
