"""Bounded routing and retries. Once content is emitted, an attempt cannot restart."""

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
import random
from time import monotonic
import tomllib
from typing import AsyncIterator, Mapping
from uuid import uuid4

import httpx

from ai_serving.backends.providers import BackendError, BackendHTTPError, BackendProtocolError
from ai_serving.contracts import ChatChunk, ChatRequest
from ai_serving.gateway.accounting import CostLedger
from ai_serving.gateway.core import Assignment, CallContext, Gateway


@dataclass(frozen=True)
class RetryContext(CallContext):
    # The caller explicitly permits repeated execution of this operation.
    # This flag is not a provider idempotency guarantee or a billing guarantee.
    retry_safe: bool = False


@dataclass(frozen=True)
class BackendPolicy:
    first_timeout: float = 2.0
    idle_timeout: float = 2.0
    max_retries: int = 1
    backoff: float = 0.02
    max_backoff: float = 0.5
    jitter: float = 0.2
    failure_threshold: int = 2
    reset_after: float = 1.0

    def __post_init__(self):
        if min(self.first_timeout, self.idle_timeout, self.reset_after) <= 0:
            raise ValueError("Backend deadlines must be positive")
        if min(self.max_retries, self.backoff, self.max_backoff, self.jitter) < 0 or self.failure_threshold < 1:
            raise ValueError("Invalid retry or circuit breaker policy")


@dataclass(frozen=True)
class Route:
    primary: tuple[str, ...]
    fallbacks: tuple[str, ...] = ()
    prompts: Mapping[str, str] = field(default_factory=dict)
    canned_reply: str | None = None
    busy_reply: str = "The service is busy. Please try again later."


@dataclass
class Circuit:
    failures: int = 0
    open_until: float = 0.0
    probe_running: bool = False

    def available(self) -> bool:
        return monotonic() >= self.open_until and not self.probe_running

    def acquire(self) -> bool:
        if not self.available():
            return False
        if self.open_until:
            self.probe_running = True
        return True

    def success(self) -> None:
        self.failures = 0
        self.open_until = 0.0
        self.probe_running = False

    def failure(self, policy: BackendPolicy) -> None:
        self.failures += 1
        if self.probe_running or self.failures >= policy.failure_threshold:
            self.open_until = monotonic() + policy.reset_after
        self.probe_running = False


def prefix_key(req: ChatRequest) -> str:
    prefix = [item for item in req.messages if item.get("role") in {"system", "developer"}]
    return hashlib.sha256(json.dumps(prefix, sort_keys=True).encode()).hexdigest()


class RoutingExhausted(BackendError):
    pass


class RoutedGateway(Gateway):
    def __init__(self, backends, routes: Mapping[str, Route], *,
                 policy: BackendPolicy | None = None,
                 backend_policies: Mapping[str, BackendPolicy] | None = None,
                 total_timeout: float = 10.0, max_attempts: int = 5,
                 prefix_aware: bool = True, ledger: CostLedger | None = None,
                 metrics=None, seed: int = 0) -> None:
        assignments = {name: Assignment(route.primary[0]) for name, route in routes.items()}
        super().__init__(backends, assignments, metrics)
        self.routes = dict(routes)
        self.policy = policy or BackendPolicy()
        self.backend_policies = dict(backend_policies or {})
        self.total_timeout = total_timeout
        self.max_attempts = max_attempts
        self.prefix_aware = prefix_aware
        self.ledger = ledger or CostLedger()
        self._owns_ledger = ledger is None
        self.circuits = {name: Circuit() for name in backends}
        self.prefixes: dict[str, set[str]] = {name: set() for name in backends}
        self.inflight = {name: 0 for name in backends}
        self._cursor = 0
        self._rng = random.Random(seed)
        self._attempt_gateways = {
            name: Gateway({name: backend}, {"default": Assignment(name)}, self.metrics)
            for name, backend in backends.items()
        }
        if total_timeout <= 0 or max_attempts < 1:
            raise ValueError("Routing deadline and attempt budget must be positive")
        for route in routes.values():
            for name in (*route.primary, *route.fallbacks):
                if name not in backends:
                    raise ValueError("Route references an unknown backend")

    @classmethod
    def from_config(cls, backends, path: str | Path, **kwargs):
        with Path(path).open("rb") as handle:
            data = tomllib.load(handle)
        defaults = BackendPolicy(**data.get("policy", {}))
        overrides = {name: replace(defaults, **values) for name, values in data.get("backends", {}).items()}
        routes = {name: Route(**{**values, "primary": tuple(values["primary"]),
                                 "fallbacks": tuple(values.get("fallbacks", []))})
                  for name, values in data["routes"].items()}
        return cls(backends, routes, policy=defaults, backend_policies=overrides,
                   **data.get("gateway", {}), **kwargs)

    def _prepare(self, req: ChatRequest, route: Route, name: str) -> ChatRequest:
        prompt = route.prompts.get(name)
        # Backend-specific fixed content goes first, user-variable content last.
        messages = ([{"role": "system", "content": prompt}] if prompt else []) + req.messages
        return replace(req, messages=messages, model=self.backends[name].profile.model)

    def _candidates(self, req: ChatRequest, route: Route) -> list[str]:
        offset = self._cursor % len(route.primary)
        self._cursor += 1
        primary = list(route.primary[offset:] + route.primary[:offset])
        if self.prefix_aware:
            primary.sort(key=lambda name: (
                prefix_key(self._prepare(req, route, name)) not in self.prefixes[name],
                self.inflight[name],
            ))
        return list(dict.fromkeys([*primary, *route.fallbacks]))

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        if isinstance(exc, BackendHTTPError):
            return exc.classified.retry_candidate
        return isinstance(exc, (httpx.TransportError, TimeoutError, BackendProtocolError))

    async def stream_chat(self, req: ChatRequest, context: CallContext | None = None) -> AsyncIterator[ChatChunk]:
        context = context or RetryContext()
        retry_safe = getattr(context, "retry_safe", False)
        # One user request can contain several calls/steps; give each call a ledger id.
        request_id = uuid4().hex
        if not context.request_id:
            context = replace(context, request_id=req.request_id or request_id)
        route = self.routes.get(req.call_type, self.routes["default"])
        deadline = monotonic() + self.total_timeout
        attempts = 0
        emitted = False
        last_error: Exception | None = None
        candidates = self._candidates(req, route)
        for candidate_index, name in enumerate(candidates):
            backend = self.backends[name]
            policy = self.backend_policies.get(name, self.policy)
            circuit = self.circuits[name]
            for retry_index in range(policy.max_retries + 1):
                if attempts >= self.max_attempts or monotonic() >= deadline:
                    break
                if not circuit.acquire():
                    self.metrics.increment("gateway_circuit_skips_total", backend=name)
                    break
                attempts += 1
                self.inflight[name] += 1
                usage = None
                status = "cancelled"
                complete = False
                failure = None
                prepared = self._prepare(req, route, name)
                progress_deadline = monotonic() + policy.first_timeout
                try:
                    stream = self._attempt_gateways[name].stream_chat(prepared, context)
                    async with aclosing(stream):
                        while True:
                            remaining = min(deadline, progress_deadline) - monotonic()
                            async with asyncio.timeout(max(0, remaining)):
                                try:
                                    chunk = await anext(stream)
                                except StopAsyncIteration:
                                    break
                            if chunk.text or chunk.reasoning:
                                emitted = True
                                progress_deadline = monotonic() + policy.idle_timeout
                            if chunk.finish_reason is not None:
                                # Even an empty completion is committed once its terminal is visible.
                                emitted = True
                                complete = True
                                usage = chunk.usage
                                status = "ok"
                            yield chunk
                    if not complete:
                        raise BackendProtocolError("Backend did not finish the response")
                    circuit.success()
                    self.prefixes[name].add(prefix_key(prepared))
                    if candidate_index:
                        self.metrics.increment("gateway_fallback_total", backend=name)
                    return
                except (asyncio.CancelledError, GeneratorExit):
                    raise
                except (BackendError, httpx.HTTPError, TimeoutError) as exc:
                    status = "error"
                    failure = last_error = exc
                    circuit.failure(policy)
                    if emitted or not self._retryable(exc):
                        raise
                finally:
                    self.inflight[name] -= 1
                    circuit.probe_running = False
                    self.ledger.record(request_id, attempts, backend, context, status, usage)
                if not retry_safe:
                    # An unmarked call is never silently executed a second time.
                    break
                if retry_index < policy.max_retries:
                    delay = min(policy.max_backoff, policy.backoff * 2 ** retry_index)
                    delay *= 1 + self._rng.uniform(0, policy.jitter)
                    if isinstance(failure, BackendHTTPError):
                        delay = max(delay, failure.classified.retry_after_seconds or 0)
                    if monotonic() + delay >= deadline:
                        break
                    self.metrics.increment("gateway_retries_total", backend=name)
                    await asyncio.sleep(delay)
            if attempts and not retry_safe:
                break
        self.metrics.increment("gateway_exhausted_total")
        # Static degradation is only for free-form replies, never fake JSON decisions.
        if req.response_format is None:
            text = route.canned_reply or route.busy_reply
            if text:
                self.metrics.increment("gateway_static_fallback_total",
                                       kind="canned" if route.canned_reply else "busy")
                model = "canned" if route.canned_reply else "busy"
                yield ChatChunk(text=text, model=model)
                yield ChatChunk(finish_reason="stop", model=model)
                return
        raise RoutingExhausted("No backend completed the request within policy") from last_error

    async def aclose(self) -> None:
        await super().aclose()
        if self._owns_ledger:
            self.ledger.close()
