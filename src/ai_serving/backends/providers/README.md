# 外部 API 后端

Phase 0 先接一个 provider，Phase 3 增加备用 provider。供应商尚未选定；适配流、取消、usage 和错误语义，不在这里决定业务路由。

已实现 `profile.py`（能力/请求编码）、`usage.py`（归一化/流式计数/价格）和 `errors.py`（错误分类）。使用配置层的 `load_profile` / `load_active_profile` 加载。当前只有 Chat Completions 请求编码，没有 HTTP/SSE transport；详见 [ProviderProfile 文档](../../../../docs/provider-profiles.md)。
