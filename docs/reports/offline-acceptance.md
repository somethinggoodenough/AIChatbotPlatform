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
| 3 | Pending | Real replicas and SGLang Gateway comparison skipped |
| 4 | Pending | Real classifier quality / judge calibration skipped |
| 5 | Pending | Real local/API delegation rates skipped |

Stored tasks and journal inputs/outputs are application state. They may contain message content and must be protected like a database. Logs, traces and benchmark reports do not contain message text, reasoning content, authorization headers or API keys.
