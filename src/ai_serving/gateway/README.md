# 模型网关

Phase 0：按 call type 选择模型并记录调用成本。Phase 3：prefix-aware routing、超时、重试预算、熔断、限流响应与 fallback。SGLang Model Gateway 仅作对照基线。

`Gateway(backends, assignments)` 接受具名 backend 和 `Assignment(backend, model)`；`load_assignments()` 从 TOML 的 `[calls.<call_type>]` 加载。选择的配置模型覆盖请求中的占位模型；空 assignment model 使用 profile 的 model。未配置的 call type 使用 default。

`stream_chat(request, context=None)` 传递 ChatChunk 并测量调用；`chat()` 累积同一条流。`CallContext` 为 task、step、user、trace 提供标识字段，不承载消息内容。`aclose()` 在应用结束时关闭 backend 连接池。Phase 0 不隐式重试。
