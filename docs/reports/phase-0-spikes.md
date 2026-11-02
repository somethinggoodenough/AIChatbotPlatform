# Phase 0 功能探针执行状态

日期：2026-11-01（用户所在时区 America/Detroit）。

四个 spike、Docker Compose 启动配置以及客户端流式 usage 开关已实现。**尚未启动用户另一台机器上的 SGLang；DeepSeek streaming + usage 已通过真实 API 验证。**

| 验证项 | 当前状态 | 原因 |
| --- | --- | --- |
| SGLang 启动 | blocked | 当前执行环境为 macOS ARM，无 NVIDIA GPU；4060 在另一台机器，尚无 SSH 地址/隧道或服务 URL |
| 本地 streaming + usage | blocked | 本地 127.0.0.1:30000 没有可用服务 |
| JSON schema | blocked | 等待 SGLang 服务 |
| Prefix cache | blocked | 等待 SGLang 服务及 cache report |
| Client disconnect | blocked | 等待 SGLang 服务及 scheduler metrics |
| DeepSeek streaming + usage | passed | `deepseek-flash` 返回 128 个内容增量、`[DONE]`、finish reason 和最终 usage 帧 |

脚本验证与真实接入证据分别记录：使用假 HTTP 服务验证读取、usage 丢失、缓存零命中及断连残留等分支，不是模型服务验收。真实结果到齐后，以 JSON 报告更新此表，并附镜像 digest、GPU/驱动版本和服务器日志；不要提前勾选阶段退出条件。

首次执行四个脚本时均退出 `2`：本地连接返回 `ConnectionRefusedError`，当时 API profile 缺少必要配置。此次 JSON 证据：

- [Streaming：local/API 分别记录](../../artifacts/spikes/20261102T022546Z-01_streaming-a23af482.json)
- [Structured output](../../artifacts/spikes/20261102T022547Z-02_structured_output-8f8c5e3e.json)
- [Prefix cache](../../artifacts/spikes/20261102T022547Z-03_prefix_cache-54a361b1.json)
- [Disconnect](../../artifacts/spikes/20261102T022547Z-04_disconnect-5ce71240.json)

配置 DeepSeek 后的首次 live 请求发现其当前模型默认开启 thinking：128 个 output tokens 全部记为 reasoning tokens，没有最终 content。该次失败证据见 [thinking 默认行为](../../artifacts/spikes/20261102T023902Z-01_streaming-5405abe2.json)。此差异随后收进 `deepseek.toml`：普通请求默认发送 `thinking.type=disabled`，显式 reasoning=`high` 时覆盖为 enabled。

修正后重新执行 API streaming spike，通过结果为：128 个 content chunks、1 个最终 usage frame、22 input tokens、128 output tokens、0 reasoning tokens、TTFT 约 0.723 秒；请求明确携带 `stream_options={"include_usage": true}`。证据见 [DeepSeek streaming + usage passed](../../artifacts/spikes/20261102T023950Z-01_streaming-31991128.json)。这些是单次功能探针数据，不作为性能基线。

统一 `DeepSeekBackend` 实现完成后又执行了一次 production transport smoke：收到 5 个 text chunks、`finish_reason=stop` 和终态 usage（14 prompt tokens / 5 completion tokens）。该 smoke 只验证共享 backend transport；取消的服务端 EOF 证据由离线 contract test 提供。

这些原始文件位于被 Git 忽略的本地 artifacts；对外分享报告时需另行归档。Compose 已通过 `config --quiet` 静态校验；这不等于镜像已拉取、容器已启动或 GPU 兼容性已验证。

操作入口：[部署](../../deploy/sglang/README.md) · [四个脚本](../../experiments/phase_0_serving_baseline/spikes/README.md)。
