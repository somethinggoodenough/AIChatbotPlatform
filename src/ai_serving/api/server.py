"""A small HTTP/1.1 adapter for the supported Chat Completions subset.

One request per connection keeps lifecycle and disconnect handling explicit.
Use a production reverse proxy for TLS and public exposure.
"""

import asyncio
from contextlib import aclosing, suppress
from dataclasses import fields
import json
import time
from typing import Any
from uuid import uuid4

import httpx

from ai_serving.backends.providers import BackendError, BackendHTTPError
from ai_serving.contracts import ChatChunk, ChatRequest
from ai_serving.gateway import Gateway
from ai_serving.gateway.routing import RetryContext
from ai_serving.guards.pipeline import GuardRejected


def wire_usage(usage: dict | None) -> dict | None:
    if usage is None:
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": None if prompt is None or completion is None else prompt + completion,
        "prompt_tokens_details": {"cached_tokens": usage.get("cached_tokens")},
        "completion_tokens_details": {"reasoning_tokens": usage.get("reasoning_tokens")},
    }


class ApiServer:
    def __init__(self, gateway: Gateway, host: str = "127.0.0.1", port: int = 0) -> None:
        self.gateway = gateway
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task] = set()

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        self.base_url = f"http://{self.host}:{self.port}"
        return self

    async def __aexit__(self, *_):
        self._server.close()
        await self._server.wait_closed()
        handlers = list(self._handlers)
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        current = asyncio.current_task()
        self._handlers.add(current)
        response_task = disconnect_task = None
        try:
            # Bound a client that connects but never sends a complete request.
            async with asyncio.timeout(10):
                header = await reader.readuntil(b"\r\n\r\n")
                lines = header.decode("latin1").split("\r\n")
                method, path, _ = lines[0].split(" ", 2)
                headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
                headers = {key.lower(): value.strip() for key, value in headers.items()}
                length = int(headers.get("content-length", "0"))
                if not 0 <= length <= 1_048_576 or "transfer-encoding" in headers:
                    raise ValueError("Unsupported body size or transfer encoding")
                body = json.loads(await reader.readexactly(length)) if length else {}
            response_task = asyncio.create_task(self._respond(method, path, body, writer))
            disconnect_task = asyncio.create_task(reader.read())
            done, _ = await asyncio.wait(
                (response_task, disconnect_task), return_when=asyncio.FIRST_COMPLETED,
            )
            if response_task in done:
                await response_task
            else:
                # Also closes an upstream that only sends keep-alive comments.
                response_task.cancel()
                await asyncio.gather(response_task, return_exceptions=True)
        except (ValueError, UnicodeDecodeError, asyncio.LimitOverrunError):
            await self._json(writer, 400, {"error": {"type": "invalid_request"}})
        except (ConnectionError, asyncio.IncompleteReadError, TimeoutError):
            pass
        finally:
            for task in (response_task, disconnect_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (response_task, disconnect_task) if task is not None),
                return_exceptions=True,
            )
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self._handlers.discard(current)

    async def _respond(self, method: str, path: str, body: dict, writer: asyncio.StreamWriter) -> None:
        if method == "GET" and path == "/metrics":
            await self._send(writer, 200, self.gateway.metrics.render().encode(), "text/plain; version=0.0.4")
            return
        if method == "GET" and path == "/health":
            await self._json(writer, 200, {"ok": True})
            return
        if method == "GET" and path == "/v1/models":
            models = sorted({item.model or self.gateway.backends[item.backend].profile.model
                             for item in self.gateway.assignments.values()})
            await self._json(writer, 200, {"object": "list", "data": [
                {"id": model, "object": "model"} for model in models
            ]})
            return
        if method != "POST" or path != "/v1/chat/completions":
            await self._json(writer, 404, {"error": {"type": "not_found"}})
            return
        streaming_started = False
        try:
            if not isinstance(body, dict) or "messages" not in body:
                raise ValueError("messages are required")
            if body.get("call_type", "default") != "default":
                raise ValueError("Internal call types are not a public API option")
            supported = {field.name for field in fields(ChatRequest)} | {"stream", "stream_options", "n", "retry_safe"}
            if body.keys() - supported or body.get("n", 1) != 1:
                raise ValueError("Unsupported Chat Completions option")
            if type(body.get("stream", False)) is not bool:
                raise ValueError("stream must be a boolean")
            if type(body.get("retry_safe", False)) is not bool:
                raise ValueError("retry_safe must be a boolean")
            options = body.get("stream_options", {})
            if not isinstance(options, dict) or type(options.get("include_usage", False)) is not bool:
                raise ValueError("stream_options must specify a boolean include_usage")
            request = ChatRequest(**{key: value for key, value in body.items()
                                     if key in {field.name for field in fields(ChatRequest)}})
            context = RetryContext(request_id=request.request_id, retry_safe=body.get("retry_safe", False))
            completion_id = "chatcmpl-" + uuid4().hex
            routed_model = self.gateway.select(request)[1].model
            envelope = {"id": completion_id, "created": int(time.time()), "model": routed_model}
            if not body.get("stream", False):
                result = await self.gateway.chat(request, context)
                envelope["model"] = result.model or routed_model
                message = {"role": "assistant", "content": result.text}
                if result.reasoning:
                    message["reasoning_content"] = result.reasoning
                await self._json(writer, 200, {
                    **envelope, "object": "chat.completion",
                    "choices": [{"index": 0, "message": message, "finish_reason": result.finish_reason}],
                    "usage": wire_usage(result.usage),
                })
                return
            async with aclosing(self.gateway.stream_chat(request, context)) as stream:
                # Keep an upstream pre-output error as an HTTP error response.
                first = await anext(stream)
                envelope["model"] = first.model or routed_model
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                             b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n")
                streaming_started = True
                await self._chunk(writer, envelope, first)
                terminal = first
                async for chunk in stream:
                    await self._chunk(writer, envelope, chunk)
                    terminal = chunk
                if body.get("stream_options", {}).get("include_usage", False):
                    await self._event(writer, {
                        **envelope, "object": "chat.completion.chunk", "choices": [],
                        "usage": wire_usage(terminal.usage),
                    })
                writer.write(b"data: [DONE]\n\n")
                await writer.drain()
        except (ValueError, TypeError):
            await self._error(writer, streaming_started, 400, "invalid_request")
        except GuardRejected:
            await self._error(writer, streaming_started, 403, "guard_rejected")
        except BackendHTTPError as exc:
            status = exc.status_code if exc.status_code >= 400 else 502
            await self._error(writer, streaming_started, status, exc.classified.kind)
        except (BackendError, httpx.HTTPError, TimeoutError):
            await self._error(writer, streaming_started, 502, "upstream_error")

    async def _error(self, writer, streaming_started: bool, status: int, kind: str) -> None:
        payload = {"error": {"type": kind}}
        if streaming_started:
            await self._event(writer, payload)
        else:
            await self._json(writer, status, payload)

    async def _chunk(self, writer, envelope: dict, chunk: ChatChunk) -> None:
        delta: dict[str, str] = {}
        if chunk.text:
            delta["content"] = chunk.text
        if chunk.reasoning:
            delta["reasoning_content"] = chunk.reasoning
        await self._event(writer, {
            **envelope, "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta, "finish_reason": chunk.finish_reason}],
        })

    @staticmethod
    async def _event(writer: asyncio.StreamWriter, payload: dict) -> None:
        writer.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
        await writer.drain()

    async def _json(self, writer: asyncio.StreamWriter, status: int, payload: Any) -> None:
        await self._send(writer, status, json.dumps(payload).encode(), "application/json")

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, status: int, body: bytes, content_type: str) -> None:
        writer.write((f"HTTP/1.1 {status} Response\r\nContent-Type: {content_type}\r\n"
                      f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
        await writer.drain()
