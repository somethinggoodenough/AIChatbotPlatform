"""Reproduce six real SIGKILL recovery windows using only the local mock."""

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import signal
import sys
import tempfile
import time

from ai_serving.contracts.messages import Message
from ai_serving.gateway.core import Assignment, Gateway
from ai_serving.runtime.workers.worker import Worker
from ai_serving.state.sqlite.store import StateStore
from ai_serving.testing.idempotent_backend import MockIdempotentBackend
from ai_serving.testing.mock_server import MockConfig, MockServer
from ai_serving.tools.registry import ToolRegistry, effect_tool


def respond(body):
    done = any(message["content"].startswith("Tool result (data):") for message in body["messages"])
    return {"action": "finish" if done else "tool", "tool": "record_effect",
            "arguments": {"name": "counter", "delta": 1}, "report": "done" if done else "", "artifacts": []}


async def run(output: Path):
    cases = []
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "recovery.db"
        store = StateStore(path)
        async with MockServer(MockConfig(ttft=0, decode_delay=0, prefill_per_token=0, responder=respond)) as server:
            gateway = Gateway({"mock": MockIdempotentBackend(server.profile())}, {"default": Assignment("mock")})
            try:
                for kind in ("model", "tool"):
                    for stage in ("before_call", "after_call", "after_journal"):
                        message = Message(f"{kind}-{stage}", "user", "fake", "one effect", mentions_bot=True)
                        store.save_message(message)
                        task_id = store.create_task(message, "one effect", ("report_contains:done",))
                        before = server.stats.generations
                        process = await asyncio.create_subprocess_exec(
                            sys.executable, "-m", "ai_serving.testing.crash_worker", "--database", str(path),
                            "--task-id", task_id, "--base-url", server.base_url, "--kind", kind, "--stage", stage,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        )
                        async with asyncio.timeout(10):
                            await process.communicate()
                        if process.returncode != -signal.SIGKILL:
                            raise RuntimeError("crash subprocess did not reach the requested boundary")
                        started = time.monotonic()
                        while store.task(task_id)["lease_until"] > time.time():
                            await asyncio.sleep(0.01)
                        prompt_before = server.stats.prompt_tokens
                        cached_before = server.stats.cached_tokens
                        replay_before = server.stats.idempotency_replays
                        worker = Worker(gateway, store, ToolRegistry([effect_tool()]))
                        report = await worker.run(task_id)
                        receipts = store.connection.execute("SELECT count(*) FROM tool_receipts WHERE task_id = ?", (task_id,)).fetchone()[0]
                        passed = report.accepted and receipts == 1 and server.stats.generations - before == 2
                        if not passed:
                            raise AssertionError("recovery acceptance failed")
                        cases.append({"kind": kind, "stage": stage, "passed": passed, "effect_count": receipts,
                                      "model_generations": server.stats.generations - before,
                                      "recovery_seconds_including_lease_wait": time.monotonic() - started,
                                      **asdict(worker.metrics),
                                      "usage_based_prefill_estimate": worker.metrics.tokens_reprefilled,
                                      "tokens_reprefilled": server.stats.prompt_tokens - prompt_before - server.stats.cached_tokens + cached_before,
                                      "provider_idempotency_replays": server.stats.idempotency_replays - replay_before})
            finally:
                await gateway.aclose()
                store.close()
    result = {"mode": "mock", "real_sigkill": True, "currency": "USD", "mock_unit_prices": 0,
              "caveat": "Mock idempotency receipts live in the parent server; server restart and paid-provider guarantees are unverified.",
              "cases": cases}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"passed": len(cases), "report": str(output), "mode": "mock"}))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/benchmarks/phase2/recovery.json"))
    asyncio.run(run(parser.parse_args().output))


if __name__ == "__main__":
    main()
