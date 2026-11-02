# SGLang 后端

`SGLangBackend` 已通过统一 `ModelBackend` 暴露 streaming、终态 usage、health 和取消传播。它复用兼容 transport，并将 health 覆盖为 SGLang server root 的 `/health`。

真实部署仍需验证 prefix cache、JSON schema 输出和断连后 scheduler request 消失。推理引擎不嵌入平台进程。
