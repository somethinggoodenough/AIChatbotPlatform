# 架构与边界

平台包含通用模型服务与 Agent runtime 两条入口；Discord 是第一个渠道。

```text
OpenAI-compatible client → api → gateway → ModelBackend → SGLang / external API
Discord → ChannelAdapter → events → Router → gateway
                              └→ subscribed Worker → gateway / tools → report → Router
Router / Worker → state → SQLite WAL
所有请求与模型调用 → observability
```

Router 拥有 persona、全局上下文和任务表，决策和回复均不携带工具。只有 Worker 可以执行工具；Worker 返回报告，最终渠道回复由 Router 协调。线程订阅让后续消息直达对应 Worker。

`contracts` 定义共享接口和消息结构，不能反向依赖具体 provider、Discord SDK 或 SQLite。`gateway` 按 call type 选择后端，承载模型层的路由和故障处理；`runtime` 承载任务调度与步骤恢复。两者通过明确请求上下文关联 task、step、trace 和成本。

状态存储是任务、订阅、session 和 journal 的事实来源。Router 的“长期运行”是逻辑角色，不意味着仅靠进程内存保存状态。SQLite WAL 是首个实现，Postgres 是后续选项。

| 阶段 | 主要模块 | 验证重点 |
| --- | --- | --- |
| 0 | contracts、backends、gateway、config、observability、benchmarks | 统一调用、流与取消、基线 |
| 1 | channels、events、runtime/router、runtime/workers、tools、state | 完整路径、线程直通、Router 无工具 |
| 2 | runtime/recovery、state/migrations | journal、replay、幂等、补偿、watchdog |
| 3 | gateway、events、benchmarks/mocks | 多后端、prefix routing、fallback、batching |
| 4 | guards、prompts、config | 并行检查、隔离、预算、失败策略 |
| 5 | experiments、benchmarks、docs/reports | 调度消融及研究报告 |

Phase 2 有一个必须在实现时澄清的边界：外部调用已经完成、结果尚未写入 journal 时，仅靠本地 journal 无法保证模型 API 永不重复收费。需验证 provider 的幂等或结果查询能力，并记录未知执行状态的恢复政策。验收报告必须说明这个窗口，不能将 mock 的幂等保证推广到所有 provider。

自研推理引擎、QQ、Postgres、Go 网关和分离 prefill/decode 均为后续扩展；当前不预设它们的实现。
