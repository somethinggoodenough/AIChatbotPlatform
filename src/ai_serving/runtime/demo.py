"""Run an offline fake conversation, or explicitly opt into a real Discord bot."""

import argparse
import asyncio
from pathlib import Path
import tempfile

from ai_serving.backends.providers.backend import ModelCompatBackend
from ai_serving.channels.fake import FakeChannel
from ai_serving.config.providers import load_profile, read_environment
from ai_serving.contracts.messages import Message
from ai_serving.events.filtering import EventFilter
from ai_serving.gateway.core import Assignment, Gateway
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


def build_runtime(gateway, store, channel, channel_ids):
    return AgentRuntime(Router(gateway, store), Worker(gateway, store, default_tools()),
                        store, channel, EventFilter(set(channel_ids)))


async def fake_demo() -> None:
    with tempfile.TemporaryDirectory() as directory:
        async with MockServer(MockConfig(responder=demo_response)) as server:
            backend = ModelCompatBackend(server.profile())
            gateway = Gateway({"mock": backend}, {"default": Assignment("mock")})
            store = StateStore(Path(directory) / "demo.db")
            channel = FakeChannel()
            runtime = build_runtime(gateway, store, channel, ["demo"])
            try:
                await runtime.handle(Message("1", "user", "demo", "hello", mentions_bot=True))
                await runtime.handle(Message("2", "user", "demo", "lookup Router", thread_id="thread", mentions_bot=True))
                await runtime.handle(Message("3", "user", "demo", "more detail", thread_id="thread"))
                print({"mode": "mock/fake", "replies": len(channel.messages), "tasks": len(store.tasks()),
                       "model_generations": server.stats.generations})
            finally:
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
    args = parser.parse_args()
    asyncio.run(discord_bot(args) if args.discord else fake_demo())


if __name__ == "__main__":
    main()
