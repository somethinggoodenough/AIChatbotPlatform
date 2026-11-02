"""Call-type routing and measurement; retries are a separate gateway policy."""

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from time import perf_counter
import tomllib
from typing import AsyncIterator, Mapping

from ai_serving.backends.providers.usage import TokenUsage
from ai_serving.contracts import ChatChunk, ChatRequest, ModelBackend
from ai_serving.observability.metrics import Metrics
from ai_serving.observability.tracing import start_span


@dataclass(frozen=True)
class Assignment:
    backend: str
    model: str = ""


@dataclass(frozen=True)
class CallContext:
    request_id: str = ""
    task_id: str = ""
    user_id: str = ""
    step_index: int | None = None
    trace_id: str = ""
    session_id: str = ""
    turn_id: str = ""


def load_assignments(path: str | Path) -> dict[str, Assignment]:
    with Path(path).open("rb") as handle:
        data = tomllib.load(handle)
    return {name: Assignment(**value) for name, value in data["calls"].items()}


class Gateway:
    def __init__(
        self,
        backends: Mapping[str, ModelBackend],
        assignments: Mapping[str, Assignment],
        metrics: Metrics | None = None,
    ) -> None:
        self.backends = dict(backends)
        self.assignments = dict(assignments)
        self.metrics = metrics or Metrics()
        if "default" not in self.assignments:
            raise ValueError("A default model assignment is required")
        for assignment in self.assignments.values():
            if assignment.backend not in self.backends:
                raise ValueError("Model assignment refers to an unknown backend")

    def select(self, req: ChatRequest) -> tuple[ModelBackend, ChatRequest]:
        assignment = self.assignments.get(req.call_type, self.assignments["default"])
        backend = self.backends[assignment.backend]
        model = assignment.model or backend.profile.model
        return backend, replace(req, model=model)

    def _record_usage(self, backend: ModelBackend, chunk: ChatChunk) -> Decimal | None:
        labels = {"backend": backend.name}
        if chunk.usage is None:
            self.metrics.increment("model_usage_unknown_total", **labels)
            return
        for kind, value in chunk.usage.items():
            if value is not None:
                self.metrics.increment("model_tokens_total", value, kind=kind, **labels)
        usage = TokenUsage(
            input_tokens=chunk.usage.get("prompt_tokens"),
            output_tokens=chunk.usage.get("completion_tokens"),
            cache_read_tokens=chunk.usage.get("cached_tokens"),
            cache_write_tokens=chunk.usage.get("cache_write_tokens"),
            reasoning_tokens=chunk.usage.get("reasoning_tokens"),
        )
        price = backend.profile.prices.estimate(usage)
        if price is None:
            self.metrics.increment("model_cost_unknown_total", **labels)
        else:
            self.metrics.increment(
                "model_cost_total", float(price), currency=backend.profile.prices.currency, **labels
            )
        return price

    async def stream_chat(
        self, req: ChatRequest, context: CallContext | None = None,
    ) -> AsyncIterator[ChatChunk]:
        started = perf_counter()
        backend, routed = self.select(req)
        labels = {"backend": backend.name, "call_type": req.call_type}
        trace = start_span("model.call", **labels)
        provider_seconds = 0.0
        last_content: float | None = None
        overhead = perf_counter() - started
        status = "cancelled"
        stream = backend.stream_chat(routed)
        try:
            async with aclosing(stream):
                while True:
                    before = perf_counter()
                    try:
                        chunk = await anext(stream)
                    except StopAsyncIteration:
                        status = "ok"
                        break
                    finally:
                        provider_seconds += perf_counter() - before
                    now = perf_counter()
                    if chunk.text or chunk.reasoning:
                        if last_content is None:
                            self.metrics.observe("model_ttft_seconds", now - started, **labels)
                        else:
                            self.metrics.observe("model_inter_chunk_seconds", now - last_content, **labels)
                        last_content = now
                    if chunk.finish_reason is not None:
                        price = self._record_usage(backend, chunk)
                        trace.set_attribute("cost_known", price is not None)
                        if price is not None:
                            trace.set_attribute("cost", float(price))
                            trace.set_attribute("currency", backend.profile.prices.currency)
                        if chunk.usage:
                            for key, value in chunk.usage.items():
                                if value is not None:
                                    trace.set_attribute(key, value)
                    overhead += perf_counter() - now
                    yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception:
            status = "error"
            self.metrics.increment("model_errors_total", **labels)
            raise
        finally:
            trace.set_attribute("outcome", status)
            trace.end()
            self.metrics.increment("model_requests_total", status=status, **labels)
            self.metrics.observe("model_provider_seconds", provider_seconds, **labels)
            self.metrics.observe("gateway_overhead_seconds", overhead, **labels)

    async def chat(self, req: ChatRequest, context: CallContext | None = None) -> ChatChunk:
        text: list[str] = []
        reasoning: list[str] = []
        result = ChatChunk()
        async with aclosing(self.stream_chat(req, context)) as stream:
            async for chunk in stream:
                text.append(chunk.text)
                reasoning.append(chunk.reasoning)
                if chunk.finish_reason is not None:
                    result = chunk
        return replace(result, text="".join(text), reasoning="".join(reasoning))

    async def aclose(self) -> None:
        for backend in self.backends.values():
            await backend.aclose()
