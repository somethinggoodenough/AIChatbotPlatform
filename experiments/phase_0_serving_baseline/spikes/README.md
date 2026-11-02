# 四个 SGLang 假设验证脚本

依赖 Python 3.11 标准库，不需要在 Mac 安装 SGLang、torch 或 provider SDK。先在 GPU 主机按 [部署说明](../../../deploy/sglang/README.md) 启动服务，再配置本机 `.env`。以下命令都从仓库根目录运行。

```sh
python3 experiments/phase_0_serving_baseline/spikes/01_streaming.py --target both
python3 experiments/phase_0_serving_baseline/spikes/02_structured_output.py
python3 experiments/phase_0_serving_baseline/spikes/03_prefix_cache.py
python3 experiments/phase_0_serving_baseline/spikes/04_disconnect.py
```

| 脚本 | 通过条件 |
| --- | --- |
| 01_streaming | 本地/API 分别收到至少两个内容增量、正常 finish、`[DONE]` 和非零 input/output usage |
| 02_structured_output | 非流式和流式均传 JSON schema；原始文本直接 `json.loads`，固定 schema 的字段、类型、枚举和范围都通过 |
| 03_prefix_cache | 4 次请求共享本轮独有的长 system prompt；后三次 cached tokens 至少 512 且占输入至少 50%，优于可观测的冷请求 |
| 04_disconnect | 独占服务上先看到 active request，中途关闭 HTTP 连接后 running/queue 在期限内稳定归零；还须明显早于自然生成对照的完成时间 |

这些是小规模功能探针，不代替后续负载测试或所有模型的能力验收。

## 流式 usage：本地与 API 都测

spike 默认测试的 SGLang 与 DeepSeek profile 都设置了 `request.stream_usage = true`，实际请求会带 `stream_options={"include_usage": true}`。脚本会读取空 `choices` 的 usage 帧，直到 `[DONE]`，不会在 finish_reason 到来时提前结束。

`--target both` 默认测试两边，分别报告结果；API 未配置时明确记录 blocked，不把本地通过算成两边通过。只测已就绪的本地服务可用 `--target local`；只测 API 用 `--target api`。API 流式请求默认上限 256 output tokens，可通过 `--max-output-tokens` 调整。

本地读取 `configs/providers/local_sglang.toml`，API 默认读取 `deepseek.toml`，不受单一 `PROVIDER_PROFILE` 选择影响。以后验证 OpenAI、Gemini、Claude 或 Grok 时，通过 `API_SPIKE_PROFILE` 指向对应 profile，不改 spike 代码。若已有模型专用 profile，也可设置 `SGLANG_SPIKE_PROFILE`。缺少 capability 或接口拒绝 `stream_options` 时必须修正/记录兼容性，不能静默重试成不带参数的请求。

## Structured output

使用 Router 风格的固定 schema：action 是 reply/spawn/ignore，reason 是字符串，priority 是 0–2 的整数，不接受额外字段。校验针对这个固定 schema 显式实现，不声称是通用 JSON Schema validator。不会剥 Markdown、补括号、提取子串或请求模型修复；拒答、截断和类型不符均失败。

## Prefix cache

每轮在 system prompt 开头放唯一 namespace，后接 64 条共享规则，再串行发送不同 user message；不调用 `flush_cache`，不影响其他缓存数据。SGLang 必须开启 `--enable-cache-report`，脚本读取 `usage.prompt_tokens_details.cached_tokens`；缺失字段不能当零命中。冷请求若因版本行为省略零命中明细，会记录未知，仍要求暖请求给出足量可观测命中。

服务日志中的 `#cached-token` 是辅助交叉检查，使用部署说明中的日志导出命令按时间窗口比对。[SGLang 源码](https://github.com/sgl-project/sglang/blob/v0.5.19/python/sglang/srt/server_args.py) 说明 `enable_cache_report` 控制 OpenAI-compatible 响应中的缓存明细。用户提供的 [NVIDIA 说明](https://build.nvidia.com/station/sglang-inference.md) 本次无法通过检索工具读取，启动参数以官方文档和源码核对为准。

## Client 断连

仅在单副本、单模型、无其他流量的 SGLang 实例上运行；通过 `/metrics` 的 `sglang:num_running_reqs` 与 `sglang:num_queue_reqs` 观察服务端状态。指标位置见 [官方 Production Metrics](https://docs.sglang.io/docs/references/production_metrics)。

先完整生成一个 3072-token 对照请求，显式设置 SGLang 的 `ignore_eos` / `min_tokens`，防止模型提前停止。再发送同样的流式请求，收到若干增量并看到 running>0 后关闭真实 HTTP 连接；不调用显式 abort API。默认 5 秒内必须观察到 running=0、queued=0，并保持至少 1 秒。

对照 decode 时长必须大于 `2 × (abort_deadline + stable_window)`，且测试请求从发出到稳定 idle 的时间必须小于对照 decode 时长的一半。若模型生成太快、提前自然结束或从未观测到 active request，则报告 inconclusive；可增加 output 上限和服务 context-length，或根据实际取消延迟调整期限。对照只能降低自然结束的混淆，不能替代任务级 tracing；正式后端还需请求 id 的生命周期追踪。

当前该脚本验证的是 SGLang 服务端请求消失。外部 API 的内部 running request 无统一观测接口，本轮只验证外部 API 的 streaming+usage，不假称已证明 provider 停止计算/计费。

## 结果与退出码

所有脚本写 `artifacts/spikes/<UTC时间>-<spike>-<run id>.json`，保存请求参数、模型、profile/probe/helper 哈希、代码 revision（若有 Git）、usage/指标及判断原因。仅包含本脚本生成的合成消息，不记录鉴权 headers 或 `.env`。服务镜像 digest 另按部署说明记录。

- `0`：所有目标通过。
- `1`：观察到假设不成立、协议错误或超时。
- `2`：配置/连接未就绪，或证据不足（inconclusive）。

离线测试使用本机假的 HTTP/SSE 服务验证脚本本身，**不算真实 SGLang / API 验收**：

```sh
uv run python -m unittest discover -s tests/unit -p 'test_*.py' -v
```

当前实际 GPU 和外部 API 结果见 [执行状态](../../../docs/reports/phase-0-spikes.md)。
