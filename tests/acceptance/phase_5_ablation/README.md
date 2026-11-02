# Phase 5: Ablation study and write-up

状态：七组 mock 配置各五次，共 35 runs 已执行；真实 local/API 模型的委派行为尚未验证。

## 验收清单

- [x] Task completion time p50/p95, number of Router decisions, prefix cache read tokens, total tokens and cost per task, and the delegation rate for (d) and (e)（仅 mock，委派策略为 fixture）
- [x] A write-up in `docs/` that states the caveats: small local models and a synthetic workload（明确未运行真实模型）
- [ ] 覆盖 (a)–(e) 配置；(d)/(e) 分别运行 local 和 API 模型，每个配置至少 5 次。

## 执行记录

```sh
uv run python -m unittest discover -s tests/integration -p 'test_ablation.py' -v
uv run python -m benchmarks.harness.ablation --runs 5
```

运行配置为 a、b、c、d-local、d-api、e-local、e-api；每组 seed 0–4。消息包含闲聊 burst、耗时任务、线程 followup 和 stop。a/b/c 使用真实 runtime、SQLite、fake channel 和 mock HTTP；d/e 是实验专用单会话 coordinator，分别执行 inline 或委派给真实 Worker。

结果位于 `artifacts/benchmarks/phase5/ablation.json` 和同目录 `ablation.md`；解释见 [消融报告](../../../docs/reports/ablation.md)、[离线验收汇总](../../../docs/reports/offline-acceptance.md)。d/e 的 local/API 标签只区分 mock backend 类和模拟延迟；脚本设定的委派率不能代表模型能力或 prompt 效果。

真实 d/e 模型对照与 Discord 测试显式 skip。GPU 恢复后先按 [运行手册](../../../docs/runbook.md) 验证 serving，再单独进行获授权的真实模型实验；不要自动开启付费 API。
