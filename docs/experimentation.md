# 实验与复现规范

`benchmarks/` 保存可复用测量代码，`experiments/` 保存研究问题与实验配置，`artifacts/` 保存本地原始输出，`docs/reports/` 保存审阅后的结论。

每个实验使用 docs/templates/experiment.md，记录假设、控制变量、实验变量、baseline、seed 和命令。必须标明真实模型 / provider 或 mock；all-local 用于无 provider 方差和 API 费用的可复现测量。

## 记录字段

记录 run id、UTC 时间、代码版本、依赖版本、硬件 / OS、模型标识与版本、配置、prompt / dataset 版本、预热方式、并发、请求数、失败 / 取消数和运行耗时。原始结果使用 JSON，图表从原始数据生成。

TTFT 需注明起止点；ITL 需注明 token 或流式 chunk 的粒度；吞吐需注明分母和 token 来源。分别报告端到端耗时、gateway 开销和 provider 耗时，并说明 provider 网络传输是否包含在测量中。记录 p50/p99 和样本量；Phase 5 另报告任务完成 p50/p95。

缓存相关实验分别控制冷 / 热状态与 prefix；provider 缓存字段只报告实际可观测值。成本记录 token usage、价格来源与计价日期，不硬编码未验证价格。

Phase 5 每种配置至少运行 5 次，(d)/(e) 分别测试 local 与 API 模型，并明确小模型、合成消息和 placeholder tool 的结论适用范围。防护实验记录 judge rubric、版本及人工校准样本。

每个正式实验最终提供一条实际可运行的复现命令，由 scripts/benchmark/ 中的入口完成负载运行、JSON 保存与绘图。当前尚未实现这些命令；不要把待填模板当成可执行步骤。
