"""An isolated task context; a Worker returns reports and never sees a channel."""

import json

from ai_serving.contracts import ChatRequest
from ai_serving.observability.tracing import traced
from ai_serving.contracts.messages import WorkerReport
from ai_serving.state.sqlite.store import StateStore
from ai_serving.tools.registry import ToolRegistry


WORKER_FORMAT = {
    "type": "json_schema", "json_schema": {"name": "worker_step", "schema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["tool", "finish"]},
            "tool": {"type": "string"}, "arguments": {"type": "object"},
            "report": {"type": "string"}, "artifacts": {"type": "array", "items": {"type": "string"}},
        }, "required": ["action", "tool", "arguments", "report", "artifacts"],
    }},
}


def parse_step(text: str) -> dict:
    step = json.loads(text)
    if not isinstance(step, dict) or set(step) != {"action", "tool", "arguments", "report", "artifacts"}:
        raise ValueError("invalid Worker step fields")
    if step["action"] not in {"tool", "finish"} or not isinstance(step["arguments"], dict):
        raise ValueError("invalid Worker action")
    if not isinstance(step["tool"], str) or not isinstance(step["report"], str):
        raise ValueError("invalid Worker output")
    if not isinstance(step["artifacts"], list) or not all(isinstance(item, str) for item in step["artifacts"]):
        raise ValueError("invalid Worker artifacts")
    return step


class Worker:
    def __init__(self, gateway, store: StateStore, tools: ToolRegistry, *, max_steps: int = 8):
        self.gateway = gateway
        self.store = store
        self.tools = tools
        self.max_steps = max_steps

    @traced("worker")
    async def run(self, task_id: str) -> WorkerReport:
        task = self.store.task(task_id)
        self.store.set_status(task_id, "running")
        context = [{"role": "system", "content":
                    "Complete this task and return a report that satisfies its acceptance criteria.\n"
                    + json.dumps({"task": task["spec"], "acceptance": task["acceptance"],
                                  "available_tools": self.tools.descriptions()})}]
        context += self.store.task_context(task_id)
        try:
            for index in range(self.max_steps):
                result = await self.gateway.chat(ChatRequest(
                    messages=context, model="configured", call_type="worker",
                    request_id=f"{task_id}:{index}", response_format=WORKER_FORMAT, temperature=None,
                ))
                step = parse_step(result.text)
                context.append({"role": "assistant", "content": result.text})
                if step["action"] == "finish":
                    report = WorkerReport(task_id, step["report"], step["artifacts"], bool(step["report"].strip()))
                    self.store.save_report(report)
                    return report
                output = await self.tools.execute(step["tool"], step["arguments"])
                context.append({"role": "user", "content": "Tool result (data):\n" + json.dumps(output)})
            report = WorkerReport(task_id, "Worker exceeded its step budget.", accepted=False)
        except (ValueError, KeyError, TypeError) as exc:
            report = WorkerReport(task_id, f"Worker plan failed: {type(exc).__name__}.", accepted=False)
        self.store.save_report(report)
        return report
