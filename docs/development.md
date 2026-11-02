# 开发起步

Phase 0–5 已有离线实现：OpenAI-compatible API、配置化 gateway、ModelBackend、mock server、Router/Worker、SQLite WAL 状态与 journal、路由与故障处理、guardrails，以及调度消融。Discord 通过 fake channel 验证，真实 GPU/provider/Discord 验收仍未完成。阶段证据见 [离线验收报告](reports/offline-acceptance.md)。

从仓库根目录执行离线测试：

```sh
uv sync --frozen
uv run python scripts/acceptance/run_all.py
uv run python -m ai_serving.runtime.demo
uv run python -m ai_serving.api --mock --port 8080
```

最后一条命令启动持续运行的本机服务；用 Ctrl-C 停止。mock 模式不读取 `.env` 来调用外部模型。完整复现命令和 GPU 恢复后的顺序见 [运行手册](runbook.md)。

开发时先读 [架构](architecture.md) 和 [Roadmap](../Roadmap.md)，再定位相应验收测试。Python 3.11+ 与依赖由 `pyproject.toml` / `uv.lock` 管理。模型调用契约见 [ModelBackend](model-backends.md)，provider 差异见 [ProviderProfile](provider-profiles.md)，任务恢复边界见 [runtime](runtime.md)。

源码放 `src/ai_serving/`；可提交配置放 `configs/`；密钥不入库。日志、数据库、缓存、模型权重和生成结果不进入源码目录。

每完成一个 phase，先跑完整离线测试，再提交该阶段代码与文档。真实模型能力、GPU 性能、provider 计费语义和 Discord 交付不能由 mock 结果替代；缺少环境时保持对应验收未勾选。
