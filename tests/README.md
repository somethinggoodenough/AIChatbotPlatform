# 测试

测试覆盖 Phase 0–5 的离线实现：真实 loopback HTTP/SSE、SQLite WAL、fake channel、租约与 fencing、SIGKILL 恢复、故障路由、防护和调度消融。真实 GPU/provider/Discord/模型质量测试带原因 skip，不计作通过。

```sh
uv run python scripts/acceptance/run_all.py
```

| 目录 | 验证范围 |
| --- | --- |
| unit/ | 配置、usage、spike 逻辑和本机 benchmark helper |
| contract/ | ModelBackend 的流、usage、reasoning、错误与取消语义 |
| integration/ | 后端、状态存储和服务组件之间的协作 |
| e2e/ | 真实渠道端到端验收的预留目录；fake-channel 路径目前在 integration/ |
| fault_injection/ | crash、超时、429、5xx、断连、挂起和循环 |
| acceptance/ | Roadmap 各阶段退出条件和证据 |
| fixtures/ | 脱敏或合成消息、工具、文档及标签 |

单独查看模型契约可运行：

```sh
uv run python -m unittest discover -s tests/contract -p 'test_model_backend.py' -v
```

执行范围见 [测试规范](../docs/testing.md)，阶段证据见 [离线验收报告](../docs/reports/offline-acceptance.md)，复现和 GPU 恢复后的命令见 [运行手册](../docs/runbook.md)。
