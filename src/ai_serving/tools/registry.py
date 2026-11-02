"""Worker-only tools, with explicit side-effect classes."""

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from ai_serving.observability.tracing import traced
from ai_serving.state.sqlite.store import Lease, StateStore


@dataclass(frozen=True)
class ToolContext:
    store: StateStore
    lease: Lease
    idempotency_key: str


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    function: Callable[..., Awaitable[Any]]
    effect: str = "read_only"
    compensation: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def __post_init__(self):
        if self.effect not in {"read_only", "idempotent", "side_effecting"}:
            raise ValueError("invalid tool effect class")
        if self.effect == "side_effecting" and self.compensation is None:
            raise ValueError("side-effecting tools must describe their compensation")


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self.tools = {tool.name: tool for tool in tools}

    def descriptions(self) -> list[dict[str, str]]:
        return [{"name": tool.name, "description": tool.description} for tool in self.tools.values()]

    @traced("worker.tool")
    async def execute(self, name: str, arguments: dict[str, Any], *, context: ToolContext | None = None) -> Any:
        if name not in self.tools:
            raise ValueError(f"unknown tool: {name}")
        tool = self.tools[name]
        if tool.effect == "read_only":
            return await tool.function(arguments)
        if context is None:
            raise ValueError("effectful tools require an idempotency key and fencing context")
        context.store.assert_lease(context.lease)
        return await tool.function(arguments, context)


def effect_tool() -> Tool:
    async def apply(arguments: dict, context: ToolContext) -> dict:
        return context.store.effect(context.lease, context.idempotency_key,
                                    str(arguments["name"]), float(arguments["delta"]))

    def compensation(arguments: dict) -> dict:
        return {"tool": "record_effect", "arguments": {"name": arguments["name"], "delta": -float(arguments["delta"])}}

    return Tool("record_effect", "Record a durable toy counter change: {name: string, delta: number}.",
                apply, "side_effecting", compensation)


def default_tools(documents: dict[str, str] | None = None) -> ToolRegistry:
    documents = documents or {
        "architecture": "The Router has no tools. Workers execute tools. SQLite stores tasks and subscriptions.",
        "serving": "SGLang serves local models. ModelBackend normalizes streaming responses and usage.",
    }

    async def rag(arguments: dict) -> list[dict[str, str]]:
        terms = set(str(arguments["query"]).lower().split())
        ranked = sorted(documents.items(), key=lambda item: -sum(term in item[1].lower() for term in terms))
        return [{"id": key, "text": text} for key, text in ranked[:3] if any(term in text.lower() for term in terms)]

    async def add(arguments: dict) -> float:
        return float(arguments["a"]) + float(arguments["b"])

    async def delay(arguments: dict) -> str:
        seconds = float(arguments.get("seconds", 0.01))
        if not 0 <= seconds <= 60:
            raise ValueError("delay must be between 0 and 60 seconds")
        await asyncio.sleep(seconds)
        return "placeholder work completed"

    return ToolRegistry([
        Tool("rag_lookup", "Search local documents: {query: string}.", rag),
        Tool("add", "Add two numbers: {a: number, b: number}.", add),
        Tool("delay", "Placeholder work: {seconds: number, at most 60}.", delay),
    ])
