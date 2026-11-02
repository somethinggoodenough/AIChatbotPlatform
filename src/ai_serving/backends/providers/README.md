# 外部 API 后端

Phase 0 先接一个 provider，Phase 3 增加备用 provider。供应商尚未选定；适配流、取消、usage 和错误语义，不在这里决定业务路由。

已实现 `profile.py`（能力/请求编码）、`usage.py`（归一化/流式计数/价格）、`errors.py`（错误分类）和 `backend.py`（异步 HTTP/SSE、取消与 health）。DeepSeek、OpenAI、Gemini 各自使用独立 backend class 和 profile。详见 [ProviderProfile](../../../../docs/provider-profiles.md) 与 [ModelBackend](../../../../docs/model-backends.md)。
