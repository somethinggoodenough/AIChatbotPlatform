"""Run the offline mock stack, or explicitly select the local SGLang profile."""

import argparse
import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
import tomllib

from ai_serving.api import ApiServer
from ai_serving.backends.providers import ModelCompatBackend
from ai_serving.backends.sglang import SGLangBackend
from ai_serving.config.providers import load_profile, read_environment
from ai_serving.gateway import Gateway, load_assignments
from ai_serving.gateway.routing import RoutedGateway
from ai_serving.testing.mock_server import MockServer


async def serve(args) -> None:
    root = Path.cwd()
    async with AsyncExitStack() as stack:
        if args.local:
            profile = load_profile(root / "configs/providers/local_sglang.toml",
                                   read_environment(root / ".env"))
            backends = {"local": SGLangBackend(profile)}
            config = root / "configs/models/all_local.toml"
        elif args.routing:
            with args.routing.open("rb") as handle:
                routing = tomllib.load(handle)
            names = {name for route in routing["routes"].values()
                     for name in route["primary"] + route.get("fallbacks", [])}
            backends = {}
            for name in names:
                mock = await stack.enter_async_context(MockServer())
                backends[name] = ModelCompatBackend(mock.profile(name))
            config = root / "configs/models/mock.toml"
        else:
            mock = await stack.enter_async_context(MockServer())
            backends = {"mock": ModelCompatBackend(mock.profile())}
            config = root / "configs/models/mock.toml"
        gateway = (RoutedGateway.from_config(backends, args.routing) if args.routing
                   else Gateway(backends, load_assignments(args.config or config)))
        stack.push_async_callback(gateway.aclose)
        api = await stack.enter_async_context(ApiServer(gateway, args.host, args.port))
        print(f"Chat API: {api.base_url}/v1/chat/completions; metrics: {api.base_url}/metrics")
        await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", help="Offline default; no .env or external API")
    mode.add_argument("--local", action="store_true", help="Use SGLang configured in .env")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--routing", type=Path, help="Phase 3 policy; --mock starts one simulator per backend")
    args = parser.parse_args()
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
