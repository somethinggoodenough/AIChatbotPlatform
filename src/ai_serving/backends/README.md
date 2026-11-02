# 模型后端

Phase 0 已实现统一 streaming、取消、health 与 token usage；本地 SGLang 和 hosted APIs 是同级实现。DeepSeek、OpenAI、Gemini 分别暴露独立 backend class，共享经过契约测试的 OpenAI-compatible transport。non-streaming 仍待实现。

未来自研引擎接入相同 `ModelBackend` 接口。
