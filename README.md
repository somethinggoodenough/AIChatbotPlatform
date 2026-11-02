# AI Chatbot Serving Platform

LLM serving 与 Agent runtime 平台。当前规划使用 SGLang 承载本地模型，外部 API 作为同级后端；Discord 是第一个工作负载。完整计划见 [Roadmap](Roadmap.md)。

**当前状态：Phase 0–5 离线版本。** serving、durable Router/Worker、多后端路由、防护与预算已贯通；调度消融已完成 35 次 mock 运行。真实 GPU、付费 provider、Discord 和模型安全验收不以 mock 替代，见 [离线验收记录](docs/reports/offline-acceptance.md)。

```text
src/ai_serving/     平台代码：接口、后端、网关、Agent runtime、渠道、状态与观测
configs/           模型分配、运行模式、策略和工作负载配置
tests/            单元、集成、契约、端到端、故障注入及分阶段验收
benchmarks/        各阶段复用的负载生成、测量和结果分析工具
experiments/       有假设、配置和结论的实验，以及 Phase 5 消融研究
docs/              架构、开发、测试、实验规范、决策和研究报告
deploy/            SGLang、应用服务和观测组件部署资料
scripts/           开发、验收与复现命令入口
artifacts/         本地生成结果，不提交原始输出
```

- [架构与模块边界](docs/architecture.md)
- [离线复现与 GPU 恢复后的执行顺序](docs/runbook.md)
- [ProviderProfile 与配置切换](docs/provider-profiles.md)
- [ModelBackend 与取消契约](docs/model-backends.md)
- [启动 SGLang](deploy/sglang/README.md) · [运行四个 spike](experiments/phase_0_serving_baseline/spikes/README.md)
- [开发起步](docs/development.md)
- [测试与验收](docs/testing.md)
- [实验与复现](docs/experimentation.md)
- [分阶段验收清单](tests/acceptance/README.md)

每阶段单独提交，离线验收与真实服务验收分别记录。真实防护验收完成前，Discord bot 仅用于私有测试服务器。

安装锁定依赖并运行离线测试（不调用外部 API；部分测试启动本机 HTTP 假服务）：

```sh
uv sync
uv run python scripts/acceptance/run_all.py
uv run python -m benchmarks.harness.baseline
uv run python -m ai_serving.runtime.demo
uv run python -m benchmarks.harness.recovery
uv run python -m benchmarks.harness.routing
uv run python -m benchmarks.harness.guards
uv run python -m benchmarks.harness.ablation --runs 5
uv run python -m ai_serving.api --mock --port 8080
```
