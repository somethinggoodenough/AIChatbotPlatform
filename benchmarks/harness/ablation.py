"""Scheduling ablation over actual runtime, SQLite, fake channel and mock HTTP.

uv run python -m benchmarks.harness.ablation --runs 5
The mock scripts delegation decisions; this does not measure model intelligence.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, replace
import json
from pathlib import Path
import random
import re
import tempfile
import time

from ai_serving.backends.providers.backend import ModelCompatBackend, SGLangBackend
from ai_serving.channels.fake import FakeChannel
from ai_serving.contracts import ChatRequest
from ai_serving.contracts.messages import Message, WorkerReport
from ai_serving.events.batching import EventBatcher
from ai_serving.events.filtering import EventFilter, TokenBucket
from ai_serving.gateway.core import Assignment, Gateway
from ai_serving.observability.metrics import percentile
from ai_serving.runtime.router.router import Router
from ai_serving.runtime.service import AgentRuntime
from ai_serving.runtime.workers.worker import Worker
from ai_serving.state.sqlite.store import StateStore
from ai_serving.testing import MockConfig, MockServer
from ai_serving.tools.registry import default_tools


CONFIGURATIONS = ("a", "b", "c", "d-local", "d-api", "e-local", "e-api")
SINGLE_FORMAT = {"type": "json_schema", "json_schema": {"name": "single_session",
    "schema": {"type": "object", "properties": {"action": {"enum": ["reply", "inline", "delegate"]}},
               "required": ["action"]}}}


def last_user(body: dict) -> str:
    return next((item["content"] for item in reversed(body["messages"]) if item["role"] == "user"), "")


async def fixed_reply(text: str):
    yield text


def responder(counts: Counter, mode: str):
    def respond(body: dict) -> str | dict:
        schema = body.get("response_format", {}).get("json_schema", {}).get("name", "reply")
        counts[schema] += 1
        user = last_user(body)
        if schema == "router_decision":
            task_id = re.search(r"task_id=([a-f0-9]+)", user)
            if user.startswith("followup") and task_id:
                return {"action": "forward_to_worker", "task": "", "acceptance": [], "task_id": task_id[1]}
            work = user.startswith("work")
            return {"action": "spawn" if work else "reply", "task": user if work else "",
                    "acceptance": ["Complete the placeholder work"] if work else [], "task_id": None}
        if schema == "worker_step":
            done = any(item["content"].startswith("Tool result (data):") for item in body["messages"])
            # The duration is a synthetic task parameter, not customer input.
            duration = re.search(r"seconds=([0-9.]+)", str(body["messages"]))
            return {"action": "finish" if done else "tool", "tool": "delay",
                    "arguments": {"seconds": float(duration[1]) if duration else 0.01},
                    "report": "Placeholder completed." if done else "", "artifacts": []}
        if schema == "single_session":
            if not user.startswith("work"):
                return {"action": "reply"}
            # Fixed fixtures make scheduling comparable; these are not learned decisions.
            delegate = mode == "e" or "work 0 " in user or "work 2 " in user
            counts["delegated"] += int(delegate)
            return {"action": "delegate" if delegate else "inline"}
        return "Mock reply."
    return respond


class SingleSessionCoordinator:
    """Experimental baseline only; deliberately not the production Router.

    Inline work occupies the session lock. A subagent releases it while a real
    Worker runs. Both paths use the same delay tool and fake output channel.
    """

    def __init__(self, gateway, worker, store, channel, tools, mode: str):
        self.gateway, self.worker, self.store = gateway, worker, store
        self.channel, self.tools, self.mode = channel, tools, mode
        self.lock = asyncio.Lock()
        self.running: dict[str, asyncio.Task] = {}

    async def handle(self, message: Message) -> str | None:
        running = None
        report = None
        async with self.lock:
            # The baseline deliberately shares one persisted conversation across threads.
            session_message = replace(message, thread_id=None)
            self.store.save_message(session_message)
            if message.text == "/stop":
                task_id = self.store.subscription(message.channel_id, message.thread_id)
                running = self.running.get(task_id)
                if running and not running.done():
                    self.store.stop(task_id, message.user_id)
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                return await self.channel.publish(message, fixed_reply("Stop request processed."))
            policy = "Prefer delegating work to the subagent tool." if self.mode == "e" else "Handle messages and use tools as needed."
            result = await self.gateway.chat(ChatRequest(
                messages=[{"role": "system", "content": policy}]
                         + self.store.session_context(session_message.session_id),
                model="configured", call_type="router_decision", response_format=SINGLE_FORMAT,
                temperature=None, request_id=message.message_id,
            ))
            action = json.loads(result.text)["action"]
            if action in {"inline", "delegate"}:
                task_id = self.store.create_task(message, message.text, ("Complete the placeholder work",))
                if action == "delegate":
                    running = asyncio.create_task(self.worker.run(task_id))
                    self.running[task_id] = running
                else:
                    duration = float(re.search(r"seconds=([0-9.]+)", message.text)[1])
                    lease = self.store.claim(task_id, "single-session", 5)
                    try:
                        await self.tools.execute("delay", {"seconds": duration})
                        report = WorkerReport(task_id, "Placeholder completed.")
                        self.store.complete(lease, report)
                    finally:
                        self.store.release(lease)
        if running:
            try:
                report = await running
            except asyncio.CancelledError:
                return None
        async with self.lock:
            context = self.store.session_context(session_message.session_id)
            if report:
                context.append({"role": "user", "content": "Worker report (data):\n" + report.text})
            stream = self.gateway.stream_chat(ChatRequest(
                messages=[{"role": "system", "content": "Reply concisely."}] + context,
                model="configured", call_type="router_reply", request_id=message.message_id,
            ))
            parts = []

            async def reply():
                try:
                    async for chunk in stream:
                        if chunk.text:
                            parts.append(chunk.text)
                            yield chunk.text
                finally:
                    await stream.aclose()
            reply_id = await self.channel.publish(message, reply())
            self.store.save_reply(session_message, "".join(parts))
            return reply_id


async def run_once(configuration: str, seed: int, delay: float = 0.03) -> dict:
    if configuration not in CONFIGURATIONS:
        raise ValueError("Unknown ablation configuration")
    rng = random.Random(seed)
    counts = Counter()
    local = configuration.endswith("local")
    mode = configuration[0]
    config = MockConfig(ttft=0.001 if local else 0.002, decode_delay=0,
                        prefill_per_token=0.000001, responder=responder(counts, mode))
    async with MockServer(config) as server:
        backend = SGLangBackend(server.profile("local-sglang")) if local else ModelCompatBackend(server.profile())
        gateway = Gateway({"mock": backend}, {"default": Assignment("mock")})
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "ablation.sqlite")
            channel = FakeChannel(edit_interval=0)
            tools = default_tools()
            worker = Worker(gateway, store, tools)
            batcher = None
            if mode in {"d", "e"}:
                runtime = SingleSessionCoordinator(gateway, worker, store, channel, tools, mode)
                handle = runtime.handle
            else:
                runtime = AgentRuntime(Router(gateway, store), worker, store, channel,
                    EventFilter({"test"}, TokenBucket(1000)),
                    thread_passthrough=mode != "b")
                batcher = EventBatcher(runtime.handle_batch, window=0 if mode == "c" else 0.008)

                async def handle(msg):
                    if store.subscription(msg.channel_id, msg.thread_id):
                        return await runtime.handle(msg)
                    return await batcher.submit(msg)
            latencies = []
            stop_latency = 0.0
            pending: list[asyncio.Task] = []

            def message(identifier: str, text: str, thread: str | None = None) -> Message:
                return Message(f"{seed}-{identifier}", "user", "test", text, thread_id=thread, mentions_bot=True)

            async def timed(msg: Message) -> None:
                started = time.perf_counter()
                await handle(msg)
                latencies.append((time.perf_counter() - started) * 1000)

            started = time.perf_counter()
            try:
                # A burst in one session exercises batching without merging distinct work tasks.
                chats = [asyncio.create_task(handle(message(f"chat-{i}", f"hello {i}"))) for i in range(3)]
                pending.extend(chats)
                work = []
                for index in range(3):
                    duration = delay * rng.uniform(0.8, 1.2)
                    task = asyncio.create_task(timed(message(f"work-{index}",
                        f"work {index} seconds={duration:.5f}", f"thread-{index}")))
                    work.append(task)
                    pending.append(task)
                await asyncio.gather(*work, *chats)
                # Completed-thread followups reuse an existing subscription. In (b) they route again.
                for index in range(2):
                    task_id = store.subscription("test", f"thread-{index}")
                    await handle(message(f"follow-{index}", f"followup task_id={task_id}", f"thread-{index}"))
                # Stop while a longer task is active. Neutral single-session work can block the stop.
                stopped = asyncio.create_task(handle(message("stop-work",
                    f"work stop seconds={delay * 4:.5f}", "stop-thread")))
                pending.append(stopped)
                async with asyncio.timeout(3):
                    while store.subscription("test", "stop-thread") is None:
                        await asyncio.sleep(0.001)
                await asyncio.sleep(delay / 3)
                stop_started = time.perf_counter()
                await handle(message("stop", "/stop", "stop-thread"))
                stop_latency = (time.perf_counter() - stop_started) * 1000
                await stopped
                elapsed = time.perf_counter() - started
                tasks = store.tasks()
                result = {"configuration": configuration, "seed": seed, "mode": "mock-only",
                    "task_latency_ms": latencies, "task_completion_p50_ms": percentile(latencies, 0.5),
                    "task_completion_p95_ms": percentile(latencies, 0.95), "elapsed_ms": elapsed * 1000,
                    "router_decisions": counts["router_decision"] + counts["single_session"],
                    "worker_model_calls": counts["worker_step"], "followups": 2, "stop_requests": 1,
                    "stop_latency_ms": stop_latency, "cancelled_tasks": sum(row["status"] == "cancelled" for row in tasks),
                    "completed_tasks": sum(row["status"] == "completed" for row in tasks),
                    "prefix_cache_read_tokens": server.stats.cached_tokens,
                    "input_tokens": server.stats.prompt_tokens, "output_tokens": server.stats.completion_tokens,
                    "total_tokens": server.stats.prompt_tokens + server.stats.completion_tokens,
                    "tokens_per_completed_task": (server.stats.prompt_tokens + server.stats.completion_tokens)
                        / max(1, sum(row["status"] == "completed" for row in tasks)),
                    "cost_per_completed_task_usd": 0.0,
                    "delegation_rate": counts["delegated"] / 4 if mode in {"d", "e"} else 1.0,
                    "channel_messages": len(channel.messages), "server": asdict(server.stats)}
            finally:
                for task in pending:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if batcher:
                    await batcher.aclose()
                await gateway.aclose()
                store.close()
            return result


def summarize(runs: list[dict]) -> list[dict]:
    result = []
    for configuration in dict.fromkeys(row["configuration"] for row in runs):
        group = [row for row in runs if row["configuration"] == configuration]
        latencies = [value for row in group for value in row["task_latency_ms"]]
        average = lambda key: sum(row[key] for row in group) / len(group)
        result.append({"configuration": configuration, "runs": len(group),
            "task_completion_p50_ms": percentile(latencies, 0.5),
            "task_completion_p95_ms": percentile(latencies, 0.95),
            **{key: average(key) for key in ("router_decisions", "prefix_cache_read_tokens", "total_tokens",
                "tokens_per_completed_task", "cost_per_completed_task_usd", "delegation_rate",
                "stop_latency_ms", "cancelled_tasks")}})
    return result


def markdown_table(rows: list[dict]) -> str:
    lines = ["| Config | Runs | Task p50 / p95 ms | Decisions/run | Cached tokens/run | Tokens/completed task | Delegation | Stop ms |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in rows:
        lines.append(f'| {row["configuration"]} | {row["runs"]} | {row["task_completion_p50_ms"]:.2f} / '
            f'{row["task_completion_p95_ms"]:.2f} | {row["router_decisions"]:.1f} | '
            f'{row["prefix_cache_read_tokens"]:.1f} | {row["tokens_per_completed_task"]:.1f} | '
            f'{row["delegation_rate"]:.2f} | {row["stop_latency_ms"]:.2f} |')
    return "\n".join(lines)


async def experiment(args) -> dict:
    runs = []
    for configuration in args.configurations:
        for seed in range(args.runs):
            runs.append(await run_once(configuration, seed, args.delay))
    result = {"mode": "mock-only", "runs": runs, "summary": summarize(runs),
        "skipped": ["Real local-model and provider delegation behavior: no GPU and paid APIs prohibited.",
                    "Real Discord delivery: fake channel only."],
        "caveats": ["Delegation decisions are scripted fixtures, not model judgments.",
                    "Mock-local/mock-api differ only in synthetic TTFT and backend class.",
                    "Cached and total tokens use the four-character mock tokenizer; prices are zero."]}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "ablation.json").write_text(json.dumps(result, indent=2) + "\n")
    (args.output / "ablation.md").write_text(markdown_table(result["summary"]) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--delay", type=float, default=0.03)
    parser.add_argument("--configurations", nargs="+", choices=CONFIGURATIONS, default=CONFIGURATIONS)
    parser.add_argument("--output", type=Path, default=Path("artifacts/benchmarks/phase5"))
    args = parser.parse_args()
    if args.runs < 5:
        parser.error("Ablation requires at least five runs per configuration")
    if not 0 < args.delay <= 1:
        parser.error("delay must be in (0, 1] seconds")
    result = asyncio.run(experiment(args))
    print("Mock scheduling experiment; scripted delegation, no real model conclusions.")
    print(markdown_table(result["summary"]))


if __name__ == "__main__":
    main()
