"""ModelBackend contract tests against a local controllable SSE server."""

import asyncio
import json
from pathlib import Path
import unittest

from ai_serving.backends.providers import (
    DeepSeekBackend,
    GeminiBackend,
    OpenAIBackend,
)
from ai_serving.backends.providers.profile import UnsupportedCapability
from ai_serving.backends.sglang import SGLangBackend
from ai_serving.config.providers import load_profile
from ai_serving.contracts import ChatRequest, ModelBackend


ROOT = Path(__file__).resolve().parents[2]


class LocalSSEServer:
    def __init__(self, mode: str = "complete") -> None:
        self.mode = mode
        self.server: asyncio.Server | None = None
        self.paths: list[str] = []
        self.bodies: list[dict] = []
        self.disconnected = asyncio.Event()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.server.close()
        await self.server.wait_closed()

    async def _request(self, reader: asyncio.StreamReader) -> tuple[str, str, dict]:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.lower()] = value.strip()
        length = int(headers.get("content-length", 0))
        raw = await reader.readexactly(length) if length else b""
        body = json.loads(raw) if raw else {}
        return method, path, body

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            method, path, body = await self._request(reader)
            self.paths.append(path)
            if body:
                self.bodies.append(body)
            if method == "GET":
                payload = b'{"ok":true}'
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                    + payload
                )
                await writer.drain()
                return
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            await self._event(writer, {"choices": [{"delta": {"content": "hel"}, "finish_reason": None}]})
            if self.mode == "hang":
                if await reader.read() == b"":
                    self.disconnected.set()
                return
            await self._event(writer, {"choices": [{"delta": {"content": "lo"}, "finish_reason": None}]})
            await self._event(writer, {"choices": [{"delta": {}, "finish_reason": "stop"}]})
            await self._event(writer, {
                "choices": [],
                "usage": {
                    "prompt_tokens": 9,
                    "completion_tokens": 2,
                    "prompt_tokens_details": {"cached_tokens": 3},
                },
            })
            writer.write(b"data: [DONE]\n\n")
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, BrokenPipeError):
            self.disconnected.set()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass

    async def _event(self, writer: asyncio.StreamWriter, value: dict) -> None:
        writer.write(b"data: " + json.dumps(value).encode() + b"\n\n")
        await writer.drain()


def profile(name: str, base_url: str):
    definitions = {
        "deepseek": (
            "deepseek.toml",
            {"DEEPSEEK_BASE_URL": base_url, "DEEPSEEK_MODEL": "deepseek-test", "DEEPSEEK_API_KEY": "test"},
        ),
        "openai": (
            "openai.toml",
            {"OPENAI_BASE_URL": base_url + "/v1", "OPENAI_MODEL": "openai-test", "OPENAI_API_KEY": "test"},
        ),
        "gemini": (
            "gemini.toml",
            {"GEMINI_BASE_URL": base_url + "/v1beta/openai", "GEMINI_MODEL": "gemini-test", "GEMINI_API_KEY": "test"},
        ),
        "local-sglang": (
            "local_sglang.toml",
            {"SGLANG_BASE_URL": base_url + "/v1", "SGLANG_MODEL": "local-test"},
        ),
    }
    filename, env = definitions[name]
    return load_profile(ROOT / "configs/providers" / filename, env)


class ModelBackendContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_contract_and_terminal_usage(self):
        async with LocalSSEServer() as server:
            backend = OpenAIBackend(profile("openai", server.base_url))
            request = ChatRequest(
                messages=[{"role": "user", "content": "hello"}],
                model="openai-test",
                max_tokens=32,
                temperature=0.2,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "answer",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {"answer": {"type": "string"}},
                            "required": ["answer"],
                            "additionalProperties": False,
                        },
                    },
                },
            )
            try:
                chunks = [chunk async for chunk in backend.stream_chat(request)]
            finally:
                await backend.aclose()
        self.assertIsInstance(backend, ModelBackend)
        self.assertEqual([chunk.text for chunk in chunks], ["hel", "lo", ""])
        self.assertTrue(all(chunk.usage is None for chunk in chunks[:-1]))
        self.assertEqual(chunks[-1].finish_reason, "stop")
        self.assertEqual(chunks[-1].usage, {
            "prompt_tokens": 9,
            "completion_tokens": 2,
            "cached_tokens": 3,
            "cache_write_tokens": 0,
            "reasoning_tokens": None,
        })
        body = server.bodies[0]
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertEqual(body["max_completion_tokens"], 32)
        self.assertEqual(body["response_format"]["type"], "json_schema")

    async def test_provider_classes_and_health_paths_are_separate(self):
        async with LocalSSEServer() as server:
            backends = [
                DeepSeekBackend(profile("deepseek", server.base_url)),
                OpenAIBackend(profile("openai", server.base_url)),
                GeminiBackend(profile("gemini", server.base_url)),
                SGLangBackend(profile("local-sglang", server.base_url)),
            ]
            try:
                self.assertEqual([await backend.health() for backend in backends], [True] * 4)
            finally:
                for backend in backends:
                    await backend.aclose()
        self.assertEqual(server.paths, [
            "/models",
            "/v1/models",
            "/v1beta/openai/models",
            "/health",
        ])

    async def test_wrong_profile_is_rejected(self):
        async with LocalSSEServer() as server:
            with self.assertRaises(ValueError):
                OpenAIBackend(profile("deepseek", server.base_url))

    async def test_structured_output_capability_is_not_silently_downgraded(self):
        schema_format = {
            "type": "json_schema",
            "json_schema": {"name": "x", "schema": {"type": "object"}},
        }
        async with LocalSSEServer() as server:
            deepseek = DeepSeekBackend(profile("deepseek", server.base_url))
            gemini = GeminiBackend(profile("gemini", server.base_url))
            try:
                with self.assertRaises(UnsupportedCapability):
                    await anext(deepseek.stream_chat(ChatRequest([], "deepseek-test", response_format=schema_format)))
                generator = gemini.stream_chat(ChatRequest([], "gemini-test", response_format=schema_format))
                first = await anext(generator)
                await generator.aclose()
                self.assertEqual(first.text, "hel")
            finally:
                await deepseek.aclose()
                await gemini.aclose()

    async def test_task_cancellation_closes_upstream_stream(self):
        async with LocalSSEServer("hang") as server:
            backend = DeepSeekBackend(profile("deepseek", server.base_url))
            first_chunk = asyncio.Event()

            async def consume():
                async for chunk in backend.stream_chat(ChatRequest([], "deepseek-test")):
                    if chunk.text:
                        first_chunk.set()

            task = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(first_chunk.wait(), 2)
                await asyncio.sleep(0.05)  # consumer is awaiting the next SSE line
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await asyncio.wait_for(server.disconnected.wait(), 2)
            finally:
                await backend.aclose()

    async def test_generator_aclose_closes_upstream_stream(self):
        async with LocalSSEServer("hang") as server:
            backend = DeepSeekBackend(profile("deepseek", server.base_url))
            generator = backend.stream_chat(ChatRequest([], "deepseek-test"))
            try:
                first = await anext(generator)
                self.assertEqual(first.text, "hel")
                await generator.aclose()
                await asyncio.wait_for(server.disconnected.wait(), 2)
            finally:
                await backend.aclose()


class ContractDataTests(unittest.TestCase):
    def test_request_validation(self):
        with self.assertRaises(ValueError):
            ChatRequest([], "", max_tokens=0)
        with self.assertRaises(ValueError):
            ChatRequest([], "model", temperature=3)


if __name__ == "__main__":
    unittest.main()
