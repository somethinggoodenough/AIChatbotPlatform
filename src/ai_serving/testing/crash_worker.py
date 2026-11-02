"""A disposable subprocess used by the real SIGKILL recovery harness."""

import argparse
import asyncio
import os
import signal

from ai_serving.gateway.core import Assignment, Gateway
from ai_serving.runtime.workers.worker import Worker
from ai_serving.state.sqlite.store import StateStore
from ai_serving.testing.idempotent_backend import MockIdempotentBackend
from ai_serving.testing.mock_server import MockServer
from ai_serving.tools.registry import ToolRegistry, effect_tool


async def run(args):
    fixture = MockServer()
    fixture.base_url = args.base_url
    backend = MockIdempotentBackend(fixture.profile())
    gateway = Gateway({"mock": backend}, {"default": Assignment("mock")})
    store = StateStore(args.database)

    def crash(stage, kind, index):
        if stage == args.stage and kind == args.kind:
            os.kill(os.getpid(), signal.SIGKILL)

    worker = Worker(gateway, store, ToolRegistry([effect_tool()]), lease_seconds=0.2,
                    progress_timeout=5, crash_hook=crash)
    try:
        await worker.run(args.task_id)
    finally:
        await gateway.aclose()
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--stage", choices=["before_call", "after_call", "after_journal"], required=True)
    parser.add_argument("--kind", choices=["model", "tool"], required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
