# Phase 0–5 offline acceptance

This implementation run has no GPU, no paid provider calls and no real Discord connection. A mock result is evidence for platform control flow, not model quality, GPU speed, provider billing or Discord delivery.

Run every available test from the repository root:

```sh
uv sync
uv run python scripts/acceptance/run_all.py
```

The runner only permits loopback network connections. Real-system acceptance is represented by named skipped tests with reasons in `tests/acceptance/test_live_requirements.py`. It never reads `.env` to start a paid call. Every phase commit is made after this entire suite passes.

| Phase | Offline implementation / evidence | External acceptance |
| --- | --- | --- |
| 0 | 76 tests run: 70 passed, 6 explicit skips; backend fixes, mock serving, API, metrics and JSON/SVG load harness | SGLang GPU and real providers skipped |
| 1 | 87 tests run: 80 passed, 7 explicit skips; 300 mock decisions, fake channel, persisted threads, Router tool boundary and complete OTel trace | Private Discord server skipped |
| 2 | 17 durability tests passed, including 6 real SIGKILL windows, fenced leases, idempotent toy effects, per-round rollback and persisted batch recovery; earlier suites green | Provider idempotency / result lookup unverified |
| 3 | 15 routing/fault tests and routed-runtime integration passed; JSON load report covers fallback, locality, re-prefill and batching | Real replicas and SGLang Gateway comparison skipped |
| 4 | 17 guard tests + 4 guarded API tests; parallel checks, immediate cancellation, structured rejection blocks tools, output buffering, budgets and 10 scripted red-team fixtures | Real classifier quality / judge calibration skipped; classifier can remain on TTFT critical path |
| 5 | 35 measured mock runs; JSON/Markdown scheduling ablation, plus opt-in local-only GPU baseline command and ordered runbook | Real local/API delegation rates skipped |

Independent archived commit checks (each snapshot imports its own source, not later worktree files): Phase 0 `3c21cd1`: 76 tests / 6 skips; Phase 1 `4559e9e`: 87 / 7; Phase 2 `997ee34`: 104 / 7; Phase 3 `8de4123`: 120 / 7; Phase 4 `b6b33de`: 141 / 7. Final offline suite runs 146 tests, with 137 passing and 9 explicitly skipped.

See [ordered commands](../runbook.md), [serving baseline](mock-baseline.md), [runtime/recovery](../runtime.md), [routing](routing.md), [guardrails](guardrails.md), and [ablation](ablation.md). The optional second-stage event filter remains unimplemented. There is no proof of real-model safety or universal exactly-once provider billing.

Stored tasks and journal inputs/outputs are application state. They may contain message content and must be protected like a database. Logs, traces and benchmark reports do not contain message text, reasoning content, authorization headers or API keys.
