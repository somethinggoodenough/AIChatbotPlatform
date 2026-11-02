# 共享接口与数据契约

`model_backend.py` 已定义 `ChatRequest`、`ChatChunk` 和 `ModelBackend`。接口只暴露统一 stream、终态 usage 和 health，不依赖具体 provider SDK。

Phase 1 的 ChannelAdapter 与统一 Message 仍待实现。
