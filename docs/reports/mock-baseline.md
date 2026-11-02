# Phase 0 offline baseline

This report validates the HTTP client and benchmark plumbing against a local
simulator. It is **not** SGLang, GPU, real-provider or Discord acceptance.

## Reproduce

```sh
uv run python -m benchmarks.harness.baseline
uv run python -m unittest discover -s tests/integration -p 'test_mock_server.py' -v
uv run python -m unittest discover -s tests/unit -p 'test_benchmark.py' -v
```

The benchmark starts its own loopback mock server, exercises the production
`ModelCompatBackend` and writes `artifacts/benchmarks/phase0/baseline.json` plus
`baseline.svg`. It does not read `.env` or contact providers. The default sweep
uses 24 requests per case, concurrency 1/4/8, input lengths 16/64/256 and output
limits 8/32, sampled with seed 42. Override those distributions with
`--input-lengths`, `--output-lengths`, `--concurrency`, `--requests` and `--seed`.

After the GPU is available and SGLang is deployed, the same harness can target
only `configs/providers/local_sglang.toml` using its values from `.env`:

```sh
uv run python -m benchmarks.harness.baseline --local --concurrency 1 2 4 --requests 24
```

This explicit mode writes to `artifacts/benchmarks/sglang/`, labels the output
`local-sglang`, records the selected model, and has no paid-provider fallback.
It has not been run against a GPU. Tests exercise that path only with the mock
server. The default command remains offline and does not read `.env`.
Real input lengths are four-character units, not exact tokenizer lengths;
reported token totals come from SGLang usage. ITL fields on real runs represent
inter-chunk gaps, since a chunk can contain multiple model tokens. Missing
usage/cache totals remain unknown instead of becoming zero. Each case against
an existing server starts with a unique fixed-length namespace, shared by that
case's requests. This separates its long workload prefix from earlier cases
without flushing other users' cache. The namespace is not written to reports;
seed-driven length sampling is unchanged. Mock cases already use separate
server instances. This isolation path is tested against mock SGLang only, not
a real GPU.

The judgment workload uses a long stable system prompt, a short varying user
prompt and a structured `action` response; every response is parsed as JSON.

## Example measured run (2026-11-02, local mock)

Milliseconds below are client-observed wall-clock timings and will vary with
machine load. Token counts use the mock's explicit four-character token rule.

| Workload | Concurrency | TTFT p50 / p99 ms | ITL p50 / p99 ms | Output tokens/s | Cache read ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| Chat | 1 | 5.83 / 19.82 | 0.59 / 0.69 | 1026.1 | 0.000 |
| Chat | 4 | 5.67 / 9.34 | 0.57 / 1.29 | 3984.3 | 0.000 |
| Chat | 8 | 7.46 / 11.64 | 0.59 / 1.27 | 6174.3 | 0.000 |
| Shared prefix | 1 | 5.33 / 11.39 | 0.58 / 0.67 | 1071.8 | 0.722 |
| Shared prefix | 4 | 5.63 / 13.93 | 0.57 / 1.03 | 3981.0 | 0.722 |
| Shared prefix | 8 | 7.48 / 16.45 | 0.60 / 1.57 | 6273.8 | 0.722 |
| Judgment | 1 | 5.35 / 11.22 | 0.59 / 0.75 | 523.0 | 0.722 |
| Judgment | 4 | 6.33 / 13.77 | 0.60 / 2.40 | 1709.2 | 0.722 |
| Judgment | 8 | 7.82 / 14.16 | 0.63 / 2.43 | 2740.8 | 0.722 |

Judgment end-to-end latency p50/p99 was 8.76/14.41 ms, 10.01/17.72 ms and
12.33/19.11 ms at concurrency 1, 4 and 8 respectively. These numbers establish
a repeatable harness, not model quality or hardware performance.

## Simulator API and fault controls

```python
from ai_serving.testing import MockConfig, MockServer

async with MockServer(MockConfig(ttft=0.02, decode_delay=0.005)) as server:
    profile = server.profile()  # For ModelCompatBackend(profile).
    server.queue_fault(status=429, retry_after=0.1)  # The next request only.
    server.queue_fault(status=503)
    server.queue_fault(disconnect_after=2)  # Emit two chunks, then close.
    server.queue_fault(hang=True)  # SSE comments continue, no model progress.
```

For a manually started server:

```sh
uv run python -m ai_serving.testing --port 8001 --ttft 0.02 --decode-delay 0.005
```

`--status 429`, `--status 503`, `--disconnect-after 2` or `--hang` apply a
persistent fault. `MockConfig.responder` accepts an in-memory callable from
request JSON to a response string or JSON object, allowing Router and guard
tests to supply deterministic fixtures. The default JSON Schema fixture
generator supports the small object/array/enum/const/primitive subset used by
tests; it is not a complete schema validator or model implementation.

Both streaming and non-streaming `/v1/chat/completions` are available, alongside
`/health`, `/v1/models` and `/metrics`. Streaming usage is included only when
`stream_options.include_usage` is true. Closing the HTTP stream cancels the
server's generation task; `server.wait_idle()` waits until active requests drop
to zero. Statistics contain counts only, with no messages or authentication
headers.

## Limits of this evidence

- Prefix caching matches the exact system/developer prefix hash. A hit removes
  simulated prefill delay for those tokens. It does not simulate GPU KV memory,
  eviction, tokenization or RadixAttention. Prefixes are admitted at request
  start, including requests later cancelled.
- Each emitted text chunk is four characters, one synthetic token; ITL therefore
  measures one-token chunk gaps in this mock. Real provider chunk timing cannot
  automatically be treated as per-token timing.
- `Idempotency-Key` replays a stable response and usage without increasing
  `stats.generations`. Reusing the key for a different request returns 409.
  Receipts are in memory: this permits worker-process crash tests while the mock
  server remains alive, not mock-server restart recovery or a guarantee about
  any real provider.
- Keep-alive comments intentionally keep the TCP stream alive while model
  progress is absent. An application progress/overall deadline is needed;
  HTTP read inactivity alone is not sufficient.
- Mock prices are zero. No inference was billed; these figures cannot establish
  actual provider costs.
- Real SGLang prefix hits, abort behavior, model JSON compliance, real API
  behavior and Discord delivery remain skipped until those systems are
  available and their tests are explicitly run.
