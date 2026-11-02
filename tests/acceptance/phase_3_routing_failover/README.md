# Phase 3: Multi-backend routing and failover

状态：未执行。下列清单来源于 [Roadmap](../../../Roadmap.md)，不表示当前功能已经实现。

## 验收清单

- [ ] During an API provider outage under load, tasks still complete through fallback. The p99 latency delta and the fallback rate are reported
- [ ] Killing a local replica under load causes no user-visible failures. The p99 TTFT delta is reported
- [ ] Our prefix-aware routing compared with the `cache_aware` baseline: prefix cache hit rate and TTFT
- [ ] Router batching: per-decision latency and the number of Router calls, with and without batching
- [ ] Resume after losing a replica: re-prefill cost, with and without routing to a replica that still holds the prefix

## 执行记录

- 代码版本 / 环境 / 模型与配置：待填写
- 运行命令与随机种子：待实现后填写
- 结果与证据路径：待填写（原始输出放 `artifacts/`）
- 未通过项 / 偏差 / 限制：待填写

完成后在此链接测试、结果 JSON 与报告；没有可运行命令或证据的项目不得勾选。
