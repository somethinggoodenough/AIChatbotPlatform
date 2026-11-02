"""Worker-only tools, with explicit side-effect classes."""

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from ai_serving.observability.tracing import traced


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    function: Callable[[dict[str, Any]], Awaitable[Any]]
    effect: str = "read_only"


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self.tools = {tool.name: tool for tool in tools}

    def descriptions(self) -> list[dict[str, str]]:
        return [{"name": tool.name, "description": tool.description} for tool in self.tools.values()]

    @traced("worker.tool")
    async def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in self.tools:
            raise ValueError(f"unknown tool: {name}")
        return await self.tools[name].function(arguments)


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
