# 平台 API

提供 OpenAI-compatible HTTP 服务边界及请求取消传播；对接网关，暴露 `/metrics`。

仓库根目录运行 `uv run python -m ai_serving.api --mock --port 8080`，默认只使用进程内启动的 mock HTTP server，不读取 `.env`。GPU 恢复后显式使用 `--local`，才加载 `.env` 中的 SGLang 配置及 `configs/models/all_local.toml`。

端点：`POST /v1/chat/completions`（流式及非流式）、`GET /v1/models`、`GET /health`、`GET /metrics`。支持一条 completion、文本、独立 reasoning、response_format、可选 temperature。不支持的参数（例如 tools）会返回 400，避免静默忽略。非流式通过累积同一条上游流实现。流式末尾只有客户端请求 include_usage 时返回 usage 帧。

这是本地实验用的最小 HTTP/1.1 服务：每条连接一个请求、最大请求体 1 MiB。默认仅监听回环地址，无鉴权、TLS 或持久交付保证；公开部署还需要这些外围能力。客户端断开会取消响应任务并关闭上游，即使上游只发送 keep-alive 注释。流中失败发送错误事件，不伪造 `[DONE]`。
