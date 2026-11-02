"""Explicit live backend probe; never part of the offline test runner."""

import argparse
import asyncio
import json
from pathlib import Path

from ai_serving.backends.providers import ModelCompatBackend
from ai_serving.config.providers import load_profile, read_environment
from ai_serving.contracts import ChatRequest


async def run(args):
    root = Path(__file__).resolve().parents[2]
    profile = load_profile(root / args.profile, read_environment(root / ".env"))
    if profile.name != "local-sglang" and not args.allow_paid:
        raise SystemExit("Nonlocal provider requires explicit --allow-paid")
    backend = ModelCompatBackend(profile)
    schema = {"type": "object", "properties": {"answer": {"type": "string"}},
              "required": ["answer"], "additionalProperties": False}
    request = ChatRequest(
        [{"role": "user", "content": "Return a JSON object with answer set to hello."}],
        profile.model,
        max_tokens=128,
        response_format={"type": "json_schema", "json_schema": {"name": "smoke", "schema": schema}},
    )
    try:
        chunks = [chunk async for chunk in backend.stream_chat(request)]
        text = "".join(chunk.text for chunk in chunks)
        parsed = json.loads(text)
        if set(parsed) != {"answer"} or not isinstance(parsed["answer"], str):
            raise RuntimeError("Streaming schema validation failed")
        result = await backend.chat(request)
        parsed = json.loads(result.text)
        if set(parsed) != {"answer"} or not isinstance(parsed["answer"], str):
            raise RuntimeError("Non-streaming schema validation failed")
        if chunks[-1].usage is None or result.usage is None:
            raise RuntimeError("Missing usage")
        print(json.dumps({"profile": profile.name, "stream_chunks": len(chunks),
                          "finish_reason": result.finish_reason, "usage": result.usage}))
    finally:
        await backend.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="configs/providers/local_sglang.toml")
    parser.add_argument("--allow-paid", action="store_true")
    asyncio.run(run(parser.parse_args()))
