# Phase 3：离线路由与故障实验

本阶段实现经过真实回环 HTTP 的 mock 测试；没有访问 GPU、付费 API 或 Discord。SGLang Model Gateway `cache_aware` 的真实对照仍然 skip，mock round-robin 不是它的替代结果。

## 重现

```sh
uv run python -m unittest discover -s tests/fault_injection -p 'test_routing.py' -v
uv run python -m benchmarks.harness.routing --requests 12 --concurrency 4
uv run python -m ai_serving.api --mock --routing configs/policies/routing.toml --port 8080
uv run python -m ai_serving.runtime.demo --routing configs/policies/routing.toml --batch-window 0.01 --retry-safe-model-calls
```

实验结果位于 `artifacts/benchmarks/phase3/routing.json`。参数包括请求数、并发数；代码固定模拟 TTFT、decode、prefix prefill 时间。mock 生成没有 API 费用。API 的可选扩展字段 `retry_safe: true` 是调用方允许重复执行的明确声明；缺省不会重试或重新生成。它不是 provider 幂等或不重复计费保证。

Runtime 同样默认禁止重复生成；可用路由配置 `[runtime].model_retry_safe` 或 mock demo 的 `--retry-safe-model-calls` 显式开启。这只作用于 Router/Worker 的只读模型调用，工具仍受 journal、幂等和 fencing 约束。Router/Worker 传入 user、session、turn、task 标识；同一 Worker revision 的多次模型调用使用固定来源消息的 turn id。多后端路由不承诺跨 provider 的 crash replay 幂等。

## 已实现的边界

- primary replicas 使用 prefix 哈希加负载排序；同优先级轮转。缓存表只保留哈希，只有完成的请求才更新缓存位置。缓存信息是提示，实际缓存可能被驱逐。
- API primary → secondary → local → canned 或 busy 的降级顺序来自配置。每个后端的固定 system prompt 可独立配置，放在变量消息之前。结构化请求绝不返回伪造的静态 JSON 决策。
- 只有明确 `retry_safe` 的操作才会再次执行；限流/临时故障使用有总次数上限的指数退避、jitter 和 `Retry-After`。鉴权与非法请求不盲重试。
- first-content、idle-progress、整条请求 deadline 分开。keep-alive 注释不能延长前两个期限。任意 text 或 reasoning 已交付后，错误直接上抛，禁止自动拼接第二次生成。
- 熔断器具有关闭、开启和单一半开探针；状态为当前进程内的路由提示，不是持久业务状态。
- `CostLedger` 用 SQLite WAL 保存每次 attempt 的 backend、user/task/request 标识、token 和 Decimal 成本。无 usage 的失败记录成本未知，不记作零。它不保存消息和密钥。
- `EventBatcher` 按 channel/thread/user 合并时间窗内事件，整个 callback 只执行一次；每个等待者得到同一处理结果，不能把共享 decision 再执行多次。订阅 Worker 的 thread 可绕过 Router batching。
- OpenAI-compatible API 在流式第一块及非流式结果中报告实际 fallback model。

## 一次参考运行（2026-11-02）

12 requests / concurrency 4：503、429、首段超时、关闭 replica、首段前断开均完成，fallback rate 为 100%。中途断开时第一批 4 个已有输出的请求失败，其后请求由熔断器避开故障后端，最终失败率 33.3%。这个行为是刻意的：已输出内容不能透明重播。

| 对照 | round-robin / 不保留 locality | prefix-aware / 保留 locality |
| --- | --- | --- |
| 缓存读取 token 占比 | 0% | 49.0% |
| TTFT p50 | 9.07 ms | 4.12 ms |
| replica 丢失后的重新 prefill | 502 synthetic tokens | 2 synthetic tokens |
| 12 个同时到达事件的 Router decision 数 | 12（无 batching） | 1（10 ms batching） |

此轮首段超时的端到端 p99 相比健康场景增加约 18.47 ms；具体 p99 TTFT delta、延迟 delta、fallback rate 都保存在 JSON。小样本结果容易受 Python 启动及调度影响，部分即时失败场景甚至得到负 delta；不能解释为故障提升性能。batching 加入的 10 ms 等待也必须计入用户延迟，不能只比较模型 decision 时间。

## 待真实环境验收

- [ ] 多个真实 SGLang replicas 的吞吐、prefix locality、abort 行为。
- [ ] 与 SGLang Model Gateway `cache_aware` 的同负载对照。
- [ ] 至少两家真实 provider 的故障边缘语义、usage 和计费。
- [ ] provider 幂等/结果查询能否消除响应已生成但客户端未持久化的重复收费窗口。

模拟器固定输出与合成 token 只用来验证路由与故障处理机制，不说明模型质量、实际 cache 命中率或 GPU 性能。
