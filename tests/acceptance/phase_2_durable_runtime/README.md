# Phase 2: Durable agent runtime

状态：以下 mock 持久执行标准已验证。测试实际终止新建 Worker 子进程，但模型服务和副作用落点是本机 mock/SQLite，不代表真实 provider 或任意第三方工具的 exactly-once 保证。

## 验收清单

- [x] At every crash point in the harness, the task completes and a side-effecting mock tool applies its effect exactly once, as checked against its idempotency log
- [x] Restarting the Router under load loses no tasks or subscriptions（离线持久 inbox、batch 和订阅恢复）
- [x] The Watchdog catches an injected hung tool and an injected loop within a configured deadline, and the user gets a report（fake channel）
- [x] Reported metrics: recovery time, steps replayed, tokens re-prefilled on resume, and API tokens and cost avoided by replay（synthetic tokens，mock API 费用为零）

## 执行记录

```sh
uv run python -m unittest discover -s tests/fault_injection -p 'test_durable_runtime.py' -v
uv run python -m benchmarks.harness.recovery
```

恢复 harness 覆盖 model/tool 各三个 SIGKILL 边界：调用前、调用后但 journal 写入前、journal 写入后。原始结果在 `artifacts/benchmarks/phase2/recovery.json`，含效果 receipts、模型实际生成次数、replay 次数与恢复时间。

实现使用 lease + fencing token 拒绝过期 Worker 写入；内置 effect sink 将幂等 receipt 与副作用放在一个事务中。补偿仅针对当前执行轮次，followup revision 和 batch membership 持久化。代码与额外回归证据见 [runtime](../../../docs/runtime.md) 和 [离线验收汇总](../../../docs/reports/offline-acceptance.md)。

- [ ] 真实 provider 的幂等/结果查询能力及计费对账。
- [ ] 第三方 effect sink 的幂等与 fencing 协议。
- [ ] 真实 Discord 部分消息交付后的恢复语义。

未知外部调用结果保持 uncertain；不具备可验证幂等的调用不会在 crash 后自动重放。mock receipts 保存在仍存活的父进程服务中，不覆盖 mock server 自身重启。真实环境步骤见 [运行手册](../../../docs/runbook.md)。
