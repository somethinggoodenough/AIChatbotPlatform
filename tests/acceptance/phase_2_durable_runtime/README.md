# Phase 2: Durable agent runtime

状态：未执行。下列清单来源于 [Roadmap](../../../Roadmap.md)，不表示当前功能已经实现。

## 验收清单

- [ ] At every crash point in the harness, the task completes and a side-effecting mock tool applies its effect exactly once, as checked against its idempotency log
- [ ] Restarting the Router under load loses no tasks or subscriptions
- [ ] The Watchdog catches an injected hung tool and an injected loop within a configured deadline, and the user gets a report
- [ ] Reported metrics: recovery time, steps replayed, tokens re-prefilled on resume, and API tokens and cost avoided by replay

## 执行记录

- 代码版本 / 环境 / 模型与配置：待填写
- 运行命令与随机种子：待实现后填写
- 结果与证据路径：待填写（原始输出放 `artifacts/`）
- 未通过项 / 偏差 / 限制：待填写

完成后在此链接测试、结果 JSON 与报告；没有可运行命令或证据的项目不得勾选。
