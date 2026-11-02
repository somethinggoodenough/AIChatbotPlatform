# Phase 0: Serving baseline and model backends

状态：离线实现与 mock 基线已验证；真实 GPU/provider 验收未完成。下列清单保留真实环境标准，不因 mock 通过而勾选。

## 验收清单

- [ ] The same client code runs against both the local and the API backend, streaming and non-streaming
- [ ] A baseline table for the local models: TTFT p50/p99, ITL p50/p99, throughput, and structured-output latency for a request the size of a Router decision
- [ ] API call latency is split into gateway overhead and provider time
- [ ] A client disconnect aborts generation on both backends (on the API side, the upstream stream is closed)

## 执行记录

```sh
uv run python -m unittest discover -s tests/contract -p 'test_model_backend.py' -v
uv run python -m unittest discover -s tests/integration -p 'test_mock_server.py' -v
uv run python -m unittest discover -s tests/integration -p 'test_gateway_api.py' -v
uv run python -m benchmarks.harness.baseline
```

已覆盖统一流式/聚合调用、usage 累积、可选 temperature、独立 reasoning、task cancel 和生成器 `aclose()`，以及平台 API 和 Prometheus 指标。mock 支持延迟、prefix cache、429/5xx、中途断连、keep-alive 挂起。离线基线默认 seed 42，保存 JSON/SVG 到 `artifacts/benchmarks/phase0/`。

证据：[mock 基线报告](../../../docs/reports/mock-baseline.md)、[离线验收汇总](../../../docs/reports/offline-acceptance.md)。真实假设探针在 [四个脚本](../../../experiments/phase_0_serving_baseline/spikes/README.md)；恢复 GPU 后按 [运行手册](../../../docs/runbook.md) 验证，再使用 `baseline --local` 获取真实服务数据。mock 数据不证明 GPU 性能或 provider 的计算/计费停止。
