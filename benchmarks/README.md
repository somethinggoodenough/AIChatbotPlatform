# 可复用基准工具

这里放跨实验复用的测量代码；具体研究假设和配置组合放 `experiments/`。

- `harness/`：负载生成、并发扫描、流式测时和统一 JSON 结果。
- `workloads/`：通用、共享前缀、judgment-style 和合成 Discord 负载。
- `mocks/`：模拟 TTFT、decode timing、usage 与故障的副本 / provider。
- `analysis/`：结果聚合、分位数、图表和对照表。

统一报告 TTFT / ITL 的 p50/p99、吞吐、token 与成本；按实验补充缓存、恢复、fallback 和调度指标。计时和分母定义见 [实验规范](../docs/experimentation.md)。
