# Phase 1: Agent vertical slice (Discord → Router → Worker → models)

状态：未执行。下列清单来源于 [Roadmap](../../../Roadmap.md)，不表示当前功能已经实现。

## 验收清单

- [ ] The Router answers chit-chat on its own. A request that needs a tool produces Worker spawn → report → Router reply
- [ ] Router decisions always parse, verified on a few hundred test messages
- [ ] The Router has no code path that executes tools. This is enforced in code and covered by a test
- [ ] A follow-up in the same thread reaches the subscribed Worker without a new Router routing decision
- [ ] Every request in the test server produces a complete trace
- [ ] The all-local mode runs the same flow end to end

> The bot stays in a private test server until the Phase 4 guardrails land.

## 执行记录

- 代码版本 / 环境 / 模型与配置：待填写
- 运行命令与随机种子：待实现后填写
- 结果与证据路径：待填写（原始输出放 `artifacts/`）
- 未通过项 / 偏差 / 限制：待填写

完成后在此链接测试、结果 JSON 与报告；没有可运行命令或证据的项目不得勾选。
