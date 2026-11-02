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
from ai_serving.gateway import Assignment, Gateway, load_assignments
from ai_serving.gateway.routing import Route, RoutedGateway
from ai_serving.guards.budget import BudgetStore
from ai_serving.guards.normalization import looks_risky
from ai_serving.guards.pipeline import GuardedGateway, ModelChecks, load_guard_policy
from ai_serving.testing.mock_server import MockConfig, MockServer


async def build_gateway(args, stack: AsyncExitStack):
    """Build the explicitly selected stack; mock mode never reads .env."""
    root = Path.cwd()
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
    classifier_name = "local"
    if args.guards and not args.local:
        classifier = await stack.enter_async_context(MockServer(MockConfig(
            responder=lambda body: {"allow": not looks_risky(body["messages"][-1]["content"])},
        )))
        classifier_name = "classifier"
        backends[classifier_name] = ModelCompatBackend(classifier.profile(classifier_name))
    assignments = load_assignments(args.config or config)
    if args.guards:
        assignments.update({name: Assignment(classifier_name) for name in ("input_classifier", "topic_classifier")})
    raw = (RoutedGateway.from_config(backends, args.routing) if args.routing
           else Gateway(backends, assignments))
    if args.guards and args.routing:
        for name in ("input_classifier", "topic_classifier"):
            raw.routes[name] = Route((classifier_name,))
            raw.assignments[name] = Assignment(classifier_name)
    gateway = raw
    if args.guards:
        args.budget_db.parent.mkdir(parents=True, exist_ok=True)
        budget = BudgetStore(args.budget_db)
        stack.callback(budget.close)
        ledger = getattr(raw, "ledger", None)
        gateway = GuardedGateway(raw, ModelChecks(raw), policy=load_guard_policy(args.guards),
                                 budget=budget, cost_reader=ledger.request_cost if ledger else None)
    stack.push_async_callback(gateway.aclose)
    return gateway


async def serve(args) -> None:
    async with AsyncExitStack() as stack:
        gateway = await build_gateway(args, stack)
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
    parser.add_argument("--guards", type=Path, help="Enable guard policy; mock uses a separate fixture classifier")
    parser.add_argument("--budget-db", type=Path, default=Path("artifacts/guard-budget.sqlite"))
    args = parser.parse_args()
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
