# Phase 0: Serving baseline and model backends

状态：未执行。下列清单来源于 [Roadmap](../../../Roadmap.md)，不表示当前功能已经实现。

## 验收清单

- [ ] The same client code runs against both the local and the API backend, streaming and non-streaming
- [ ] A baseline table for the local models: TTFT p50/p99, ITL p50/p99, throughput, and structured-output latency for a request the size of a Router decision
- [ ] API call latency is split into gateway overhead and provider time
- [ ] A client disconnect aborts generation on both backends (on the API side, the upstream stream is closed)

## 执行记录

- 代码版本 / 环境 / 模型与配置：待填写
- 运行命令与随机种子：待实现后填写
- 结果与证据路径：待填写（原始输出放 `artifacts/`）
- 未通过项 / 偏差 / 限制：待填写

完成后在此链接测试、结果 JSON 与报告；没有可运行命令或证据的项目不得勾选。

功能假设探针已实现：[四个脚本](../../../experiments/phase_0_serving_baseline/spikes/README.md)、[执行状态](../../../docs/reports/phase-0-spikes.md)。当前未连接真实 GPU / API，不能勾选以上验收项。
