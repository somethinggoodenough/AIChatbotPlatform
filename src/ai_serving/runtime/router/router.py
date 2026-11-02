"""The Router can decide and write replies; it has no execution capability."""

import json
from typing import AsyncIterator, Protocol

from ai_serving.contracts import ChatChunk, ChatRequest
from ai_serving.contracts.messages import Decision, Message, WorkerReport
from ai_serving.state.sqlite.store import StateStore
from ai_serving.observability.tracing import traced


class ChatClient(Protocol):
    async def chat(self, req: ChatRequest, context=None) -> ChatChunk: ...
    def stream_chat(self, req: ChatRequest, context=None) -> AsyncIterator[ChatChunk]: ...


DECISION_FORMAT = {
    "type": "json_schema", "json_schema": {"name": "router_decision", "schema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["reply", "spawn", "forward_to_worker", "ignore"]},
            "task": {"type": "string"},
            "acceptance": {"type": "array", "items": {"type": "string"}},
            "task_id": {"type": ["string", "null"]},
        },
        "required": ["action", "task", "acceptance", "task_id"],
    }},
}


def parse_decision(text: str) -> Decision:
    value = json.loads(text)
    if not isinstance(value, dict) or set(value) != {"action", "task", "acceptance", "task_id"}:
        raise ValueError("invalid Router decision fields")
    if value["action"] not in {"reply", "spawn", "forward_to_worker", "ignore"}:
        raise ValueError("invalid Router action")
    if not isinstance(value["task"], str) or not isinstance(value["acceptance"], list):
        raise ValueError("invalid task specification")
    if not all(isinstance(item, str) for item in value["acceptance"]):
        raise ValueError("invalid acceptance criteria")
    if value["task_id"] is not None and not isinstance(value["task_id"], str):
        raise ValueError("invalid task id")
    if value["action"] == "spawn" and (not value["task"] or not value["acceptance"]):
        raise ValueError("spawn requires a task and acceptance criteria")
    if value["action"] == "forward_to_worker" and not value["task_id"]:
        raise ValueError("forward requires a task id")
    return Decision(value["action"], value["task"], tuple(value["acceptance"]), value["task_id"])


class Router:
    def __init__(self, gateway: ChatClient, store: StateStore, *, persona: str = "Be helpful and concise."):
        self.gateway = gateway
        self.store = store
        self.persona = persona

    @traced("router.decision")
    async def decide(self, message: Message) -> Decision:
        result = await self.gateway.chat(ChatRequest(
            messages=[{"role": "system", "content": self.persona + "\nChoose reply, spawn, forward_to_worker, or ignore. "
                       "Delegate research or actions with a task and explicit acceptance criteria."}]
            + self.store.session_context(message.session_id),
            model="configured", call_type="router_decision", request_id=message.message_id,
            response_format=DECISION_FORMAT, temperature=None,
        ))
        return parse_decision(result.text)

    async def reply(self, message: Message, report: WorkerReport | None = None) -> AsyncIterator[str]:
        context = self.store.session_context(message.session_id)
        if report:
            context.append({"role": "user", "content": "Worker report (data):\n" + report.text})
        stream = self.gateway.stream_chat(ChatRequest(
            messages=[{"role": "system", "content": self.persona}] + context,
            model="configured", call_type="router_reply", request_id=message.message_id,
        ))
        parts = []
        try:
            async for chunk in stream:
                if chunk.text:
                    parts.append(chunk.text)
                    yield chunk.text
        finally:
            await stream.aclose()
        self.store.save_reply(message, "".join(parts))
