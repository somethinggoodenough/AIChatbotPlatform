# 分阶段验收

以 Roadmap 为阶段退出标准。各目录区分离线已验证行为与仍缺真实环境的验收项；mock 通过不能勾选真实 GPU、provider、Discord 或模型能力标准。

```sh
uv run python scripts/acceptance/run_all.py
```

统一入口只允许 loopback 连接，并报告带原因的真实环境 skip。详细结果见 [离线验收报告](../../docs/reports/offline-acceptance.md)，实验复现和 GPU 恢复顺序见 [运行手册](../../docs/runbook.md)。

- [Phase 0: Serving baseline and model backends](phase_0_serving_baseline/README.md)
- [Phase 1: Agent vertical slice (Discord → Router → Worker → models)](phase_1_agent_vertical_slice/README.md)
- [Phase 2: Durable agent runtime](phase_2_durable_runtime/README.md)
- [Phase 3: Multi-backend routing and failover](phase_3_routing_failover/README.md)
- [Phase 4: Guardrails and abuse resistance](phase_4_guardrails/README.md)
- [Phase 5: Ablation study and write-up](phase_5_ablation/README.md)
