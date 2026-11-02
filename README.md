# AI Chatbot Serving Platform

LLM serving 与 Agent runtime 平台。当前规划使用 SGLang 承载本地模型，外部 API 作为同级后端；Discord 是第一个工作负载。完整计划见 [Roadmap](Roadmap.md)。

**当前状态：ProviderProfile + Phase 0 功能探针。** 已有配置、请求编码、usage/错误归一化、价格估算、四个 HTTP spike 和 SGLang Docker Compose 配置；生产后端、平台服务和完整基准框架尚未实现。真实 GPU / API 验证等待远程服务与配置，见 [执行状态](docs/reports/phase-0-spikes.md)。Python 3.11+ / asyncio 是当前默认。

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
- [ProviderProfile 与配置切换](docs/provider-profiles.md)
- [启动 SGLang](deploy/sglang/README.md) · [运行四个 spike](experiments/phase_0_serving_baseline/spikes/README.md)
- [开发起步](docs/development.md)
- [测试与验收](docs/testing.md)
- [实验与复现](docs/experimentation.md)
- [分阶段验收清单](tests/acceptance/README.md)

从 Phase 0 开始实现。完成一个阶段时，同步更新 README、验收证据和可复现命令。Phase 4 防护验收完成前，Discord bot 仅用于私有测试服务器。

当前离线测试（无额外依赖、无外部网络调用；部分测试启动本机 HTTP 假服务）：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests/unit -p 'test_*.py' -v
```
