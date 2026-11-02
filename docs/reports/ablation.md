# Phase 5 offline scheduling ablation

This experiment exercises scheduling through real local HTTP connections,
SQLite state, the production Router/Worker runtime and a fake channel. It does
not evaluate a language model's ability to choose delegation, and it is not a
reproduction of real DeepSeek model-quality results.

## Reproduce

```sh
uv run python -m benchmarks.harness.ablation --runs 5
```

The command writes individual runs and summaries to
`artifacts/benchmarks/phase5/ablation.json` and a table to `ablation.md` in the
same directory. It starts its own mock servers, uses temporary SQLite databases,
does not read `.env`, and never calls a paid provider or Discord. Five seeds are
used per configuration. `--delay` changes the placeholder work duration and
`--configurations` can select a subset; the CLI requires at least five runs.

## Configurations and workload

| Configuration | Scheduling |
| --- | --- |
| a | Production Router/Worker, thread passthrough, 8 ms event batching |
| b | Same runtime and batching, but followups need a new Router decision |
| c | Same runtime and thread passthrough, with batching disabled |
| d-local / d-api | Experimental single-session coordinator, neutral prompt, a subagent action plus inline work |
| e-local / e-api | Same single-session coordinator, delegate-first prompt |

Each run sends a burst of three chit-chat messages, three work requests in
separate threads, two followups to completed tasks, then a longer work request
and a stop request while that work is active. The three initial work requests
are timed from submission through channel delivery. Followups and the stop
request have separate counters; stop response latency is reported separately.

The a/b/c configurations use `AgentRuntime`, `Router`, `Worker`, `EventBatcher`,
`StateStore`, `FakeChannel` and the real HTTP `ModelCompatBackend`. The d/e
baseline lives only in the benchmark: it has one shared persisted conversation
and one session lock. Inline placeholder execution holds that lock; invoking
the subagent action starts a real Worker task and releases the lock. This
deliberately lets inline work delay subsequent messages, including a stop.
The production Router never receives or executes tools.

## Interpretation and limitations

- All model replies are deterministic fixtures served by the mock HTTP server.
  The neutral fixture delegates work 0 and 2 and runs work 1 and the long stop
  task inline. The delegate-first fixture delegates all four. Delegation rate
  therefore verifies that the chosen policy was exercised; it is not evidence
  that a particular prompt or model improves delegation.
- `local` uses `SGLangBackend` against the mock and a 1 ms simulated first-token
  delay; `api` uses `ModelCompatBackend` and 2 ms. Neither uses an actual model.
- Timing includes HTTP parsing, runtime scheduling, SQLite operations, fake
  channel delivery and the configured delay tool. No GPU computation or
  Discord network latency is represented. Timings vary with machine load.
- Prefix cache hits are exact shared-system-prefix matches in the simulator.
  Tokens use four-character chunks. Input/output totals come from mock server
  counters; partial cancelled output without final usage is not a real billing
  estimate. Tokens per completed task are run totals divided by completed task
  count, including the overhead of chatter and followups.
- Cost per completed task is zero because no paid inference occurs. Real
  provider prices and real tokens must be used before drawing cost conclusions.
- Work completion percentiles pool the three measured task latencies from each
  seed. Each configuration's five runs provide only fifteen samples; p95 should
  not be interpreted as a stable production tail-latency estimate.
- Real local/API runs for d/e are explicitly skipped until GPU/provider access
  is authorized. Real Discord behavior is also skipped. These acceptance items
  remain unchecked in the Roadmap.

The generated JSON records every run rather than only averages, so the results
can be re-aggregated without repeating inference. No message bodies or secrets
are written into the benchmark artifacts.

## Measured mock run: 2026-11-02

Command: `uv run python -m benchmarks.harness.ablation --runs 5` (35 runs).
All rows below were measured through the implementation, not assigned expected
latencies. Cost per completed task was USD 0 for every row. The JSON also
contains each run's total input/output tokens and completion/cancellation counts.

| Config | Runs | Task p50 / p95 ms | Decisions/run | Cached tokens/run | Tokens/completed task | Delegation | Stop ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| a | 5 | 128.09 / 239.94 | 5.0 | 1222.0 | 910.1 | 1.00 | 2.49 |
| b | 5 | 101.71 / 111.26 | 7.0 | 1298.0 | 989.4 | 1.00 | 1.39 |
| c | 5 | 110.22 / 138.24 | 7.0 | 1310.0 | 969.7 | 1.00 | 1.57 |
| d-local | 5 | 81.74 / 115.65 | 9.0 | 424.0 | 415.8 | 0.50 | 111.38 |
| d-api | 5 | 119.02 / 169.86 | 9.0 | 424.0 | 415.8 | 0.50 | 111.75 |
| e-local | 5 | 85.47 / 94.65 | 9.0 | 740.0 | 704.9 | 1.00 | 2.25 |
| e-api | 5 | 89.73 / 119.64 | 9.0 | 740.0 | 704.9 | 1.00 | 0.53 |

Thread passthrough and batching each removed two Router decisions in this
fixture. Fewer calls did not produce a clear latency win in the small workload:
batching itself adds an 8 ms collection window and observed tails were noisy.
This is a tradeoff to measure under a real workload, not a reason to claim that
batching always lowers latency.

The neutral coordinator's long inline task completed before it could process
the stop message (zero tasks cancelled); the other configurations cancelled
one task per run. The approximately 111 ms stop delay in d is the deliberate
session-lock behavior. The d/e delegation difference is scripted, so this
result supports the responsiveness of separate Worker scheduling, not a claim
about neutral versus delegate-first prompts on real models.

Token totals across a/b/c compare the same production flow. Comparing their
totals directly with d/e also changes context layout and followup behavior:
the shared single session replies from its conversation, while subscribed
production Workers execute another task revision. Do not interpret that
cross-architecture difference as a model-efficiency improvement.
