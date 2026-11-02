"""Small HTTP/SSE model simulator. Timings and token counts are synthetic.

It deliberately serves one request per TCP connection. No request bodies or
authentication headers are retained in statistics or written to logs.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import json
import math
from typing import Any, Callable

from ai_serving.backends.providers.profile import Capabilities, ProviderProfile, RequestMapping
from ai_serving.backends.providers.usage import PriceTable


@dataclass
class MockFault:
    status: int = 200
    disconnect_after: int | None = None
    hang: bool = False
    retry_after: float = 0.01


@dataclass
class MockConfig:
    ttft: float = 0.005
    decode_delay: float = 0.001
    prefill_per_token: float = 0.00001
    keepalive_interval: float = 0.01
    cache_enabled: bool = True
    response_text: str = "This is a deterministic mock model response."
    reasoning: str = ""
    fault: MockFault = field(default_factory=MockFault)
    # Called only in memory; it can return a JSON object for structured output.
    responder: Callable[[dict[str, Any]], str | dict[str, Any]] | None = None


@dataclass
class MockStats:
    requests: int = 0
    generations: int = 0
    completed: int = 0
    cancelled: int = 0
    active_requests: int = 0
    cache_hits: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    idempotency_replays: int = 0


def synthetic_tokens(text: str) -> int:
    """One synthetic token per four characters; NOT a model tokenizer."""
    return math.ceil(len(text) / 4)


def schema_example(schema: dict[str, Any]) -> Any:
    """Generate a fixture for a small JSON Schema subset, not model validation."""
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    kind = schema.get("type")
    if kind == "object":
        properties = schema.get("properties", {})
        return {key: schema_example(properties[key]) for key in schema.get("required", properties)}
    if kind == "array":
        return [schema_example(schema.get("items", {})) for _ in range(schema.get("minItems", 0))]
    if kind in {"number", "integer"}:
        return schema.get("minimum", 0)
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    return "mock"


class MockServer:
    def __init__(self, config: MockConfig | None = None) -> None:
        self.config = config or MockConfig()
        self.stats = MockStats()
        self.faults: deque[MockFault] = deque()
        self._cache: set[str] = set()
        self._responses: dict[str, tuple[str, dict[str, Any], str]] = {}
        self._request_hashes: dict[str, str] = {}
        self._handlers: set[asyncio.Task] = set()
        self._server: asyncio.Server | None = None
        self.base_url = ""

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> MockServer:
        self._server = await asyncio.start_server(self._handle, host, port)
        actual_port = self._server.sockets[0].getsockname()[1]
        self.base_url = f"http://{host}:{actual_port}/v1"
        return self

    async def __aenter__(self) -> MockServer:
        return await self.start()

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        pending = list(self._handlers)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def wait_idle(self, timeout: float = 1.0) -> None:
        async with asyncio.timeout(timeout):
            while self.stats.active_requests:
                await asyncio.sleep(0.001)

    def queue_fault(self, **kwargs: Any) -> None:
        self.faults.append(MockFault(**kwargs))

    def clear_cache(self) -> None:
        self._cache.clear()

    def profile(self, name: str = "mock") -> ProviderProfile:
        zero = Decimal(0)
        return ProviderProfile(
            name=name, model="mock-model", base_url=self.base_url,
            capabilities=Capabilities(streaming=True, json_schema=True,
                                      strict_json_schema=True, temperature=True, reasoning=True),
            request=RequestMapping(stream_usage=True),
            prices=PriceTable(input_per_million=zero, output_per_million=zero,
                              cache_read_per_million=zero, cache_write_per_million=zero),
        )

    async def _json(self, writer: asyncio.StreamWriter, status: int, body: dict,
                    extra_headers: str = "") -> None:
        data = json.dumps(body).encode()
        headers = (f"HTTP/1.1 {status} Mock\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {len(data)}\r\nConnection: close\r\n{extra_headers}\r\n")
        writer.write(headers.encode() + data)
        await writer.drain()

    def _result(self, body: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
        messages = body.get("messages", [])
        prefix = "\n".join(str(item.get("content", "")) for item in messages
                           if item.get("role") in {"system", "developer"})
        all_text = "\n".join(str(item.get("content", "")) for item in messages)
        prompt_tokens = synthetic_tokens(all_text)
        cache_key = hashlib.sha256(prefix.encode()).hexdigest()
        cached = synthetic_tokens(prefix) if prefix and cache_key in self._cache else 0
        if not self.config.cache_enabled:
            cached = 0
        elif prefix:
            self._cache.add(cache_key)
        if self.config.responder:
            output = self.config.responder(body)
        elif body.get("response_format", {}).get("type") == "json_schema":
            output = schema_example(body["response_format"]["json_schema"]["schema"])
        elif body.get("response_format", {}).get("type") == "json_object":
            output = {"reply": "mock"}
        else:
            output = self.config.response_text
        text = json.dumps(output, ensure_ascii=False) if isinstance(output, dict) else output
        limit = body.get("max_completion_tokens", body.get("max_tokens", 512))
        finish = "length" if synthetic_tokens(text) > limit else "stop"
        text = text[:limit * 4]
        reasoning_tokens = synthetic_tokens(self.config.reasoning)
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": synthetic_tokens(text) + reasoning_tokens,
            "prompt_tokens_details": {"cached_tokens": cached},
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        }
        self.stats.generations += 1
        self.stats.cache_hits += int(cached > 0)
        self.stats.prompt_tokens += prompt_tokens
        self.stats.cached_tokens += cached
        return text, usage, finish

    async def _generate(self, writer: asyncio.StreamWriter, body: dict[str, Any],
                        fault: MockFault, idempotency_key: str) -> bool:
        if fault.status != 200:
            code = "rate_limit" if fault.status == 429 else "server_error"
            await self._json(writer, fault.status, {"error": {"code": code}},
                             f"Retry-After: {fault.retry_after}\r\n")
            return False
        request_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if idempotency_key and idempotency_key in self._request_hashes:
            if self._request_hashes[idempotency_key] != request_hash:
                await self._json(writer, 409, {"error": {"code": "idempotency_conflict"}})
                return False
        if idempotency_key and idempotency_key in self._responses:
            text, usage, finish = self._responses[idempotency_key]
            self.stats.idempotency_replays += 1
        else:
            text, usage, finish = self._result(body)
            if idempotency_key:
                self._request_hashes[idempotency_key] = request_hash
                self._responses[idempotency_key] = (text, usage, finish)
        uncached = usage["prompt_tokens"] - usage["prompt_tokens_details"]["cached_tokens"]
        stream = body.get("stream", False)
        if stream:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
            await writer.drain()
        if fault.hang:
            while True:
                if stream:
                    writer.write(b": keep-alive\n\n")
                    await writer.drain()
                await asyncio.sleep(self.config.keepalive_interval)
        await asyncio.sleep(self.config.ttft + self.config.prefill_per_token * uncached)
        if not stream:
            message = {"role": "assistant", "content": text}
            if self.config.reasoning:
                message["reasoning_content"] = self.config.reasoning
            await self._json(writer, 200, {"id": "mock", "object": "chat.completion",
                "model": body.get("model"), "choices": [{"index": 0, "message": message,
                "finish_reason": finish}], "usage": usage})
        else:
            async def emit(event: dict[str, Any]) -> None:
                writer.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                await writer.drain()

            sent = 0
            if self.config.reasoning:
                await emit({"choices": [{"index": 0, "delta": {
                    "reasoning_content": self.config.reasoning}, "finish_reason": None}]})
            for offset in range(0, len(text), 4):
                if fault.disconnect_after is not None and sent >= fault.disconnect_after:
                    return False
                await emit({"choices": [{"index": 0, "delta": {"content": text[offset:offset + 4]},
                                          "finish_reason": None}]})
                sent += 1
                await asyncio.sleep(self.config.decode_delay)
            await emit({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
            if body.get("stream_options", {}).get("include_usage"):
                await emit({"choices": [], "usage": usage})
            writer.write(b"data: [DONE]\n\n")
            await writer.drain()
        self.stats.completion_tokens += usage["completion_tokens"]
        self.stats.completed += 1
        return True

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handler = asyncio.current_task()
        self._handlers.add(handler)
        generation = disconnected = None
        active = False
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            lines = header.decode().split("\r\n")
            method, path, _ = lines[0].split()
            headers = {key.lower(): value.strip() for key, value in
                       (line.split(":", 1) for line in lines[1:] if ":" in line)}
            if path in {"/health", "/v1/models"}:
                await self._json(writer, 200, {"data": [{"id": "mock-model", "object": "model"}]})
                return
            if path == "/metrics":
                metrics = "\n".join(f"mock_{name} {value}" for name, value in vars(self.stats).items())
                metrics += f"\nsglang:num_running_reqs {self.stats.active_requests}\nsglang:num_queue_reqs 0\n"
                data = metrics.encode()
                writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: {len(data)}\r\n"
                              "Connection: close\r\n\r\n").encode() + data)
                await writer.drain()
                return
            if method != "POST" or path != "/v1/chat/completions":
                await self._json(writer, 404, {"error": {"code": "not_found"}})
                return
            payload = await reader.readexactly(int(headers.get("content-length", "0")))
            body = json.loads(payload)
            self.stats.requests += 1
            self.stats.active_requests += 1
            active = True
            fault = self.faults.popleft() if self.faults else self.config.fault
            generation = asyncio.create_task(self._generate(writer, body, fault,
                headers.get("idempotency-key", "").strip()))
            # Once the body is read, EOF means the HTTP client closed its stream.
            disconnected = asyncio.create_task(reader.read())
            done, _ = await asyncio.wait({generation, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if generation in done:
                await generation
            else:
                self.stats.cancelled += 1
        except (asyncio.IncompleteReadError, ConnectionError, json.JSONDecodeError):
            pass
        finally:
            for task in (generation, disconnected):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (generation, disconnected) if task), return_exceptions=True)
            if active:
                self.stats.active_requests -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self._handlers.discard(handler)


async def _serve(host: str, port: int, config: MockConfig) -> None:
    server = await MockServer(config).start(host, port)
    print(f"Mock OpenAI-compatible server: {server.base_url}")
    try:
        await asyncio.Event().wait()
    finally:
        await server.aclose()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--ttft", type=float, default=0.005)
    parser.add_argument("--decode-delay", type=float, default=0.001)
    parser.add_argument("--status", type=int, default=200)
    parser.add_argument("--disconnect-after", type=int)
    parser.add_argument("--hang", action="store_true")
    args = parser.parse_args()
    config = MockConfig(ttft=args.ttft, decode_delay=args.decode_delay,
                        fault=MockFault(status=args.status, disconnect_after=args.disconnect_after, hang=args.hang))
    try:
        asyncio.run(_serve(args.host, args.port, config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
