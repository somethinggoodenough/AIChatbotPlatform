# Phase 4: Guardrails and abuse resistance

状态：离线防护控制流、取消、缓冲、规范化、预算和故障策略已验证；真实模型安全性、真实 judge 校准及公开 Discord 安全验收未执行。

## 验收清单

- [ ] Guardrail overhead on p50/p99 TTFT is measured and reported. Target: the classifier is never on the critical path
- [ ] The red-team pass rate is reported per category
- [x] With the classifier killed, topic checks fall back to rules and safety checks block, as verified by tests（mock 故障注入）

## 执行记录

```sh
uv run python -m unittest discover -s tests/integration -p 'test_guards.py' -v
uv run python -m benchmarks.harness.guards
```

输入检查与生成并行启动；检查完成前缓冲输出，拒绝和调用方取消会关闭上游流。缓冲、canary、reasoning 检查、会话 turn/token/spend 限制与 fail-open/fail-closed 策略通过 mock 测试。配置在 `configs/policies/guards.toml`，合成攻击集在 `configs/workloads/red_team.json`。

证据：[防护报告](../../../docs/reports/guardrails.md)、[离线验收汇总](../../../docs/reports/offline-acceptance.md)。实验测量 mock 的 TTFT 开销和按类别 fixture 结果；固定 mock judge 不是已校准的真实 LLM judge，不能据此勾选真实 red-team 通过率。

慢 classifier 或输出缓冲可能位于 TTFT 关键路径，因此“never on the critical path”目标仍未满足。真实验证步骤与付费预算边界见 [运行手册](../../../docs/runbook.md)。
