# Phase 1: Agent vertical slice (Discord → Router → Worker → models)

状态：fake channel → Router → Worker → mock HTTP 路径已验证；私有 Discord 与真实模型验收尚未执行。

## 验收清单

- [ ] The Router answers chit-chat on its own. A request that needs a tool produces Worker spawn → report → Router reply
- [ ] Router decisions always parse, verified on a few hundred test messages
- [x] The Router has no code path that executes tools. This is enforced in code and covered by a test (`test_router_has_no_tool_execution_path`)
- [ ] A follow-up in the same thread reaches the subscribed Worker without a new Router routing decision
- [ ] Every request in the test server produces a complete trace
- [ ] The all-local mode runs the same flow end to end

其余标准涉及真实渠道或模型行为，保留未勾选。fake 输出能解析不等于真实模型几百条消息都正确。真实安全验收前不要开放公共 Discord。

## 执行记录

```sh
uv run python -m unittest discover -s tests/integration -p 'test_agent_runtime.py' -v
uv run python -m ai_serving.runtime.demo
```

离线证据包括：闲聊回复、Worker 工具执行与报告、300 个 mock decision 解析、线程 followup 绕过 Router、SQLite 订阅重启恢复、完整 OpenTelemetry trace、all-local assignment 指向 mock SGLang，以及长消息节流编辑。Worker 不持有 channel，Router 不导入或执行工具。

结果与边界见 [离线验收汇总](../../../docs/reports/offline-acceptance.md)、[runtime 说明](../../../docs/runtime.md)。真实 Discord 连接测试显式 skip；接入真实环境的顺序见 [运行手册](../../../docs/runbook.md)。
