# ModelBackend contract

`ModelBackend` is the gateway-facing boundary for model calls. The canonical types live in `src/ai_serving/contracts/model_backend.py`; HTTP/provider details stay behind backend implementations.

```python
backend: ModelBackend
async for chunk in backend.stream_chat(request):
    if chunk.text:
        send_downstream(chunk.text)
    if chunk.finish_reason:
        record_usage(chunk.usage)
```

`ChatChunk.usage` appears only on the terminal chunk, together with `finish_reason`. It uses canonical keys: `prompt_tokens`, `completion_tokens`, `cached_tokens`, `cache_write_tokens`, and `reasoning_tokens`. A cancelled or interrupted stream may have no terminal usage because providers often send usage immediately before `[DONE]`.

Usage is assembled with `profile.usage_accumulator()` before normalization: cumulative snapshots merge without double counting; delta increments sum. `temperature=None` omits the parameter. `ChatChunk.reasoning` carries `reasoning_content` separately from user-facing `text`. `chat()` collects the same stream into one result, preserving reasoning, finish reason and usage.

## Implementations

| Class | Profile | Health endpoint |
| --- | --- | --- |
| `SGLangBackend` | `local_sglang.toml` | `/health` on the SGLang server root |
| `DeepSeekBackend` | `deepseek.toml` | OpenAI-compatible `/models` |
| `OpenAIBackend` | `openai.toml` | `/v1/models` |
| `GeminiBackend` | `gemini.toml` | `/v1beta/openai/models` |

The three hosted providers subclass `ModelCompatBackend` but remain separate public classes. Provider-specific request shape, capability, usage and error differences stay in their profiles or subclass overrides. Adding Claude or Grok means adding a profile and a separate backend class after its protocol is verified.

## Cancellation contract

`stream_chat()` holds the upstream `httpx.Response` inside an async generator. Its `finally` block explicitly awaits `response.aclose()`.

- Cancelling the task while it awaits the next SSE event runs the `finally` block and closes the upstream connection.
- Calling `aclose()` on the async generator while it is suspended at a yielded chunk runs the same cleanup.
- Closing a completed stream is idempotent.
- Closing the reusable backend connection pool is separate: call `await backend.aclose()` during process shutdown.

This guarantees that this process stops consuming the upstream stream. A hosted provider may already have performed or billed work before it observes the closed connection; cancellation is not a refund or exactly-once guarantee.

The contract tests use a real local TCP server. It sends one SSE chunk and then waits; the test passes only when the server observes EOF after task cancellation or generator `aclose()`.

## Structured output

`ChatRequest.response_format` carries the OpenAI Chat Completions wire shape. `json_object` is passed through. `json_schema` requires the selected profile to declare support; `strict: true` requires strict schema support. Unsupported formats raise `UnsupportedCapability` and are never silently downgraded.

- SGLang: JSON Schema enabled in its profile, pending live GPU verification.
- OpenAI: JSON Schema and strict mode enabled; the exact model must also support them.
- Gemini: JSON Schema enabled through its OpenAI compatibility API; strict mode remains disabled until live subset testing.
- DeepSeek: current documented JSON Object mode is not treated as JSON Schema enforcement.

## Run tests

```sh
uv sync
uv run python -m unittest discover -s tests/unit -p 'test_*.py' -v
uv run python -m unittest discover -s tests/contract -p 'test_*.py' -v
```

The current contract tests are offline and never call a hosted provider. Live provider and SGLang probes remain separate experiments.

## Protocol references

- [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create): SSE and `stream_options.include_usage` terminal usage chunk.
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs): JSON Schema subset and strict output behavior.
- [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai): compatibility base URL, streaming, usage option and structured output.
- [DeepSeek API](https://api-docs.deepseek.com/): OpenAI-format base URL and Chat Completions endpoint.
