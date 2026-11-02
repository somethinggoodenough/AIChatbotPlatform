# Phase 3: Multi-backend routing and failover

状态：多 mock backend 的路由、重试、熔断、降级、成本记录与 batching 已验证。真实 provider/replica 与 SGLang Gateway 对照仍未执行。

## 验收清单

- [ ] During an API provider outage under load, tasks still complete through fallback. The p99 latency delta and the fallback rate are reported
- [ ] Killing a local replica under load causes no user-visible failures. The p99 TTFT delta is reported
- [ ] Our prefix-aware routing compared with the `cache_aware` baseline: prefix cache hit rate and TTFT
- [x] Router batching: per-decision latency and the number of Router calls, with and without batching（mock 测量）
- [ ] Resume after losing a replica: re-prefill cost, with and without routing to a replica that still holds the prefix

## 执行记录

```sh
uv run python -m unittest discover -s tests/fault_injection -p 'test_routing.py' -v
uv run python -m unittest discover -s tests/integration -p 'test_routed_runtime.py' -v
uv run python -m benchmarks.harness.routing --requests 12 --concurrency 4
```

mock 故障负载覆盖 429、503、首段超时、keep-alive 挂起、断开 replica、首段前断开与输出中断。首段后禁止透明重试；因此中途断开允许明确失败，不能伪装成完全无损。报告记录失败率、fallback rate、p99 delta、prefix locality、重新 prefill 与 batching 成本。

证据：[路由报告](../../../docs/reports/routing.md)、`artifacts/benchmarks/phase3/routing.json`、[离线验收汇总](../../../docs/reports/offline-acceptance.md)。mock round-robin 是内部对照，不是 SGLang Gateway `cache_aware` 实测。真实环境顺序见 [运行手册](../../../docs/runbook.md)。
