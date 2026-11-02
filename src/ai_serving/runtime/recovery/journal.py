"""Replay completed calls; uncertain external calls require explicit idempotency."""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ai_serving.state.sqlite.store import Lease, StateStore
from ai_serving.gateway.core import Gateway


def supports_step_replay(gateway, request) -> bool:
    # A routed/wrapped gateway may choose a different replica on the next call.
    # Verification currently covers only a fixed plain Gateway assignment.
    if type(gateway) is not Gateway:
        return False
    backend, _ = gateway.select(request)
    return getattr(backend, "supports_idempotency", False)


class UncertainOutcome(RuntimeError):
    """The operation may have completed; automatic repetition is not justified."""


class StepFailure(ValueError):
    pass


@dataclass
class RecoveryMetrics:
    seconds: float = 0.0
    steps_replayed: int = 0
    tokens_avoided: int = 0
    tokens_reprefilled: int = 0
    cost_avoided: float = 0.0
    cost_unknown_steps: int = 0


class Journal:
    def __init__(self, store: StateStore, lease: Lease, metrics: RecoveryMetrics,
                 crash_hook: Callable[[str, str, int], None] | None = None):
        self.store = store
        self.lease = lease
        self.index = lease.start_step
        self.metrics = metrics
        self.crash_hook = crash_hook or (lambda stage, kind, index: None)

    async def call(self, kind: str, backend: str, value: Any,
                   operation: Callable[[str], Awaitable[Any]], *, replay_safe: bool = False,
                   compensation: dict | None = None) -> Any:
        index = self.index
        previous = self.store.step(self.lease.task_id, index)
        if previous and (previous["kind"] != kind or previous["backend"] != backend or previous["input"] != value):
            raise ValueError("replay diverged from the persisted step")
        self.store.assert_lease(self.lease)
        if previous and previous["status"] == "failed":
            self.index += 1
            raise StepFailure(previous["output"]["error"])
        if previous and previous["status"] == "completed":
            self.index += 1
            self.metrics.steps_replayed += 1
            if kind == "model":
                usage = previous["output"].get("usage") or {}
                self.metrics.tokens_avoided += (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
                cost = previous["output"].get("cost")
                if cost is None:
                    self.metrics.cost_unknown_steps += 1
                else:
                    self.metrics.cost_avoided += cost
            return previous["output"]
        if previous and not replay_safe:
            raise UncertainOutcome("step outcome is unknown and the backend has no verified idempotency")
        step = self.store.begin_step(self.lease, index, kind, backend, value, compensation)
        self.crash_hook("before_call", kind, index)
        try:
            output = await operation(step["idempotency_key"])
        except (ValueError, KeyError, TypeError) as exc:
            output = {"error": type(exc).__name__}
            self.store.finish_step(self.lease, index, output, status="failed")
            self.index += 1
            raise StepFailure(output["error"]) from None
        self.crash_hook("after_call", kind, index)
        self.store.finish_step(self.lease, index, output)
        self.crash_hook("after_journal", kind, index)
        self.index += 1
        if kind == "model":
            usage = output.get("usage") or {}
            self.metrics.tokens_reprefilled += max(0, (usage.get("prompt_tokens") or 0) - (usage.get("cached_tokens") or 0))
        return output
