"""Run an offline fake conversation, or explicitly opt into a real Discord bot."""

import argparse
import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
import tempfile
import tomllib

from ai_serving.backends.providers.backend import ModelCompatBackend
from ai_serving.channels.fake import FakeChannel
from ai_serving.config.providers import load_profile, read_environment
from ai_serving.contracts.messages import Message
from ai_serving.events.filtering import EventFilter
from ai_serving.events.batching import EventBatcher
from ai_serving.gateway.core import Assignment, Gateway
from ai_serving.gateway.routing import RoutedGateway
from ai_serving.runtime.router.router import Router
from ai_serving.runtime.service import AgentRuntime
from ai_serving.runtime.workers.worker import Worker
from ai_serving.state.sqlite.store import StateStore
from ai_serving.testing.mock_server import MockConfig, MockServer
from ai_serving.tools.registry import default_tools


def demo_response(body):
    name = body.get("response_format", {}).get("json_schema", {}).get("name")
    if name == "router_decision":
        work = "lookup" in body["messages"][-1]["content"]
        return {"action": "spawn" if work else "reply", "task": "Find the Router boundary" if work else "",
                "acceptance": ["Explain the Router boundary"] if work else [], "task_id": None}
    if name == "worker_step":
        done = any(item["content"].startswith("Tool result (data):") for item in body["messages"])
        return {"action": "finish" if done else "tool", "tool": "rag_lookup", "arguments": {"query": "Router"},
                "report": "Only Workers execute tools." if done else "", "artifacts": []}
    return "Mock reply: only Workers execute tools."


def build_runtime(gateway, store, channel, channel_ids, *, model_retry_safe=False):
    return AgentRuntime(Router(gateway, store, model_retry_safe=model_retry_safe),
                        Worker(gateway, store, default_tools(), model_retry_safe=model_retry_safe),
                        store, channel, EventFilter(set(channel_ids)))


async def fake_demo(routing: Path | None = None, batch_window: float = 0,
                    model_retry_safe: bool = False) -> None:
    with tempfile.TemporaryDirectory() as directory:
        async with AsyncExitStack() as stack:
            names = {"mock"}
            if routing:
                with routing.open("rb") as handle:
                    config = tomllib.load(handle)
                names = {name for route in config["routes"].values()
                         for name in route["primary"] + route.get("fallbacks", [])}
                model_retry_safe |= config.get("runtime", {}).get("model_retry_safe", False)
            servers, backends = {}, {}
            for name in names:
                server = await stack.enter_async_context(MockServer(MockConfig(responder=demo_response)))
                servers[name] = server
                backends[name] = ModelCompatBackend(server.profile(name))
            gateway = (RoutedGateway.from_config(backends, routing) if routing
                       else Gateway(backends, {"default": Assignment("mock")}))
            store = StateStore(Path(directory) / "demo.db")
            channel = FakeChannel()
            runtime = build_runtime(gateway, store, channel, ["demo"], model_retry_safe=model_retry_safe)
            batcher = EventBatcher(runtime.handle_batch, batch_window)
            try:
                await asyncio.gather(*(batcher.submit(Message(str(i), "user", "demo", text, mentions_bot=True))
                                       for i, text in ((1, "hello"), (2, "one more thought"))))
                await batcher.submit(Message("work", "user", "demo", "lookup Router", thread_id="thread", mentions_bot=True))
                # A subscribed follow-up bypasses Router batching entirely.
                await runtime.handle(Message("3", "user", "demo", "more detail", thread_id="thread"))
                print({"mode": "mock/fake", "replies": len(channel.messages), "tasks": len(store.tasks()),
                       "model_generations": sum(server.stats.generations for server in servers.values()),
                       "batch_window": batch_window, "routing": routing is not None})
            finally:
                await batcher.aclose()
                await gateway.aclose()
                store.close()


async def discord_bot(args) -> None:
    from ai_serving.channels.discord.adapter import DiscordAdapter, create_client

    environment = read_environment(Path(".env"))
    token = environment.get("DISCORD_BOT_TOKEN")
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN must be set for --discord")
    if not args.profile or not args.channel_id:
        raise ValueError("--discord requires --profile and at least one --channel-id")
    profile = load_profile(args.profile, environment)
    gateway = Gateway({profile.name: ModelCompatBackend(profile)}, {"default": Assignment(profile.name)})
    store = StateStore(args.database)
    client = create_client()
    adapter = DiscordAdapter(client)
    runtime = build_runtime(gateway, store, adapter, args.channel_id)

    @client.event
    async def on_message(event):
        await runtime.handle(adapter.normalize(event))

    try:
        async with client:
            await client.start(token)
    finally:
        await gateway.aclose()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discord", action="store_true", help="Explicitly contact the real Discord API")
    parser.add_argument("--profile", help="Provider TOML; used only with --discord")
    parser.add_argument("--channel-id", action="append", default=[])
    parser.add_argument("--database", default="runtime.db")
    parser.add_argument("--routing", type=Path, help="Offline mock routing policy")
    parser.add_argument("--batch-window", type=float, default=0, help="Offline event batching window in seconds")
    parser.add_argument("--retry-safe-model-calls", action="store_true",
                        help="Explicitly permit read-only model generation retries; not provider billing deduplication")
    args = parser.parse_args()
    if args.discord and (args.routing or args.batch_window or args.retry_safe_model_calls):
        parser.error("Routing/batching demo options are offline-only")
    if args.batch_window < 0:
        parser.error("batch-window cannot be negative")
    asyncio.run(discord_bot(args) if args.discord else fake_demo(
        args.routing, args.batch_window, args.retry_safe_model_calls))


if __name__ == "__main__":
    main()
