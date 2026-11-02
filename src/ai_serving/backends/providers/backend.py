"""Async OpenAI-compatible model backends with explicit stream cleanup."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
from typing import Any, AsyncIterator, Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx

from ai_serving.contracts import ChatChunk, ChatRequest

from .errors import ClassifiedError
from .profile import ProviderProfile, UnsupportedCapability
from .usage import TokenUsage


class BackendError(RuntimeError):
    """Base class for normalized backend failures."""


class BackendProtocolError(BackendError):
    """The upstream returned a malformed or incomplete stream."""


class BackendHTTPError(BackendError):
    def __init__(self, backend: str, status_code: int, classified: ClassifiedError) -> None:
        super().__init__(f"{backend} returned HTTP {status_code} ({classified.kind})")
        self.backend = backend
        self.status_code = status_code
        self.classified = classified


def _health_url(base_url: str, path: str) -> str:
    parsed = urlsplit(base_url)
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class ModelCompatBackend:
    """OpenAI Chat Completions compatible transport.

    Provider subclasses remain separate so protocol quirks do not leak into
    gateway or runtime code. A caller must eventually call ``aclose()`` on the
    backend itself to release the reusable connection pool.
    """

    expected_profile_name: str | None = None

    def __init__(
        self,
        profile: ProviderProfile,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
    ) -> None:
        if self.expected_profile_name and profile.name != self.expected_profile_name:
            raise ValueError(
                f"{type(self).__name__} requires profile {self.expected_profile_name!r}, "
                f"got {profile.name!r}"
            )
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.profile = profile
        self.name = profile.name
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _request_body(self, req: ChatRequest) -> dict[str, Any]:
        body = self.profile.build_request(
            req.messages,
            stream=True,
            max_output_tokens=req.max_tokens,
            temperature=req.temperature,
        )
        # The canonical request selects the model after gateway routing. The
        # selected profile still defines that model's capabilities and usage.
        body["model"] = req.model
        if req.response_format is not None:
            body["response_format"] = self._response_format(req.response_format)
        return body

    def _response_format(self, value: Mapping[str, Any]) -> dict[str, Any]:
        result = deepcopy(dict(value))
        kind = result.get("type")
        if kind == "json_object":
            return result
        if kind != "json_schema":
            raise UnsupportedCapability(f"{self.name} does not support response format {kind!r}")
        if not self.profile.capabilities.json_schema:
            raise UnsupportedCapability(f"{self.name} does not support JSON Schema output")
        descriptor = result.get("json_schema")
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("schema"), dict):
            raise ValueError("json_schema response_format requires json_schema.schema")
        if descriptor.get("strict") is True and not self.profile.capabilities.strict_json_schema:
            raise UnsupportedCapability(f"{self.name} does not support strict JSON Schema output")
        return result

    def _request_headers(self, req: ChatRequest) -> dict[str, str]:
        # request_id stays internal until a provider's documented correlation
        # header is configured; arbitrary headers are not assumed compatible.
        return self.profile.request_headers()

    async def _close_response(self, response: httpx.Response) -> None:
        """Finish network cleanup even when the consumer task is cancelled."""
        close_task = asyncio.create_task(response.aclose())
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            # A second cancellation must not leave the upstream stream open.
            await close_task
            raise

    async def _sse_payloads(self, response: httpx.Response) -> AsyncIterator[str]:
        data: list[str] = []
        async for line in response.aiter_lines():
            if line == "":
                if data:
                    yield "\n".join(data)
                    data.clear()
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                value = line[5:]
                data.append(value[1:] if value.startswith(" ") else value)
        if data:
            yield "\n".join(data)

    def _canonical_usage(self, normalized: TokenUsage) -> dict[str, int | None] | None:
        if all(value is None for value in asdict(normalized).values()):
            return None
        return {
            "prompt_tokens": normalized.input_tokens,
            "completion_tokens": normalized.output_tokens,
            "cached_tokens": normalized.cache_read_tokens,
            "cache_write_tokens": normalized.cache_write_tokens,
            "reasoning_tokens": normalized.reasoning_tokens,
        }

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        response: httpx.Response | None = None
        saw_done = False
        finish_reason: str | None = None
        usage = self.profile.usage_accumulator()
        try:
            request = self._client.build_request(
                "POST",
                self.profile.url,
                headers=self._request_headers(req),
                json=self._request_body(req),
                timeout=self._timeout,
            )
            response = await self._client.send(request, stream=True)
            if not response.is_success:
                try:
                    body = await response.aread()
                    parsed = json.loads(body) if body else {}
                    if not isinstance(parsed, dict):
                        parsed = {}
                except (json.JSONDecodeError, UnicodeDecodeError):
                    parsed = {}
                classified = self.profile.error_classifier.classify(
                    status=response.status_code,
                    body=parsed,
                    headers=response.headers,
                )
                raise BackendHTTPError(self.name, response.status_code, classified)
            content_type = response.headers.get("content-type", "").lower()
            if "text/event-stream" not in content_type:
                raise BackendProtocolError(f"{self.name} did not return an SSE stream")

            async for payload in self._sse_payloads(response):
                if payload == "[DONE]":
                    saw_done = True
                    break
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise BackendProtocolError(f"{self.name} returned invalid SSE JSON") from exc
                if not isinstance(event, dict):
                    raise BackendProtocolError(f"{self.name} returned a non-object SSE event")
                if "error" in event or event.get("type") == "error":
                    classified = self.profile.error_classifier.classify(status=200, body=event)
                    raise BackendHTTPError(self.name, 200, classified)
                if event.get("usage") is not None:
                    if not isinstance(event["usage"], dict):
                        raise BackendProtocolError(f"{self.name} returned invalid usage")
                    usage.update(event["usage"])
                choices = event.get("choices", [])
                if not isinstance(choices, list):
                    raise BackendProtocolError(f"{self.name} returned invalid choices")
                if len(choices) > 1:
                    raise BackendProtocolError("The canonical backend currently supports one choice")
                if choices:
                    choice = choices[0]
                    if not isinstance(choice, dict):
                        raise BackendProtocolError(f"{self.name} returned an invalid choice")
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        raise BackendProtocolError(f"{self.name} returned an invalid delta")
                    text = delta.get("content")
                    reasoning = delta.get("reasoning_content")
                    text = "" if text is None else text
                    reasoning = "" if reasoning is None else reasoning
                    if not isinstance(text, str) or not isinstance(reasoning, str):
                        raise BackendProtocolError(f"{self.name} returned non-text content")
                    if text or reasoning:
                        yield ChatChunk(text=text, reasoning=reasoning, model=req.model)
                    if choice.get("finish_reason") is not None:
                        if not isinstance(choice["finish_reason"], str):
                            raise BackendProtocolError(f"{self.name} returned invalid finish_reason")
                        finish_reason = choice["finish_reason"]

            if not saw_done:
                raise BackendProtocolError(f"{self.name} stream ended before [DONE]")
            if finish_reason is None:
                raise BackendProtocolError(f"{self.name} stream ended without finish_reason")
            yield ChatChunk(
                finish_reason=finish_reason,
                usage=self._canonical_usage(usage.normalized(self.profile.usage_normalizer)),
                model=req.model,
            )
        finally:
            # Both task cancellation while awaiting bytes and generator.aclose()
            # while suspended at yield execute this block.
            if response is not None:
                await self._close_response(response)

    async def chat(self, req: ChatRequest) -> ChatChunk:
        """Non-streaming callers collect the same checked streaming protocol."""
        result = ChatChunk()
        stream = self.stream_chat(req)
        try:
            async for chunk in stream:
                result.text += chunk.text
                result.reasoning += chunk.reasoning
                result.model = chunk.model
                if chunk.finish_reason is not None:
                    result.finish_reason = chunk.finish_reason
                    result.usage = chunk.usage
        finally:
            await stream.aclose()
        return result

    async def health(self) -> bool:
        try:
            response = await self._client.get(
                self.profile.base_url.rstrip("/") + "/models",
                headers=self.profile.request_headers(),
                timeout=min(self._timeout, 10.0),
            )
            try:
                return response.is_success
            finally:
                await response.aclose()
        except httpx.HTTPError:
            return False


class DeepSeekBackend(ModelCompatBackend):
    expected_profile_name = "deepseek"


class OpenAIBackend(ModelCompatBackend):
    expected_profile_name = "openai"


class GeminiBackend(ModelCompatBackend):
    expected_profile_name = "gemini"


class SGLangBackend(ModelCompatBackend):
    expected_profile_name = "local-sglang"

    async def health(self) -> bool:
        try:
            response = await self._client.get(
                _health_url(self.profile.base_url, "/health"),
                headers=self.profile.request_headers(),
                timeout=min(self._timeout, 10.0),
            )
            try:
                return response.is_success
            finally:
                await response.aclose()
        except httpx.HTTPError:
            return False
