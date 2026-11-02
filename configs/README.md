# 配置

配置样例应能公开提交，不包含密钥、用户消息或真实账号信息。ProviderProfile 已采用 TOML；密钥和部署地址由根目录 `.env` 或进程环境提供。其余配置类型仍待实现。

- `models/`：各 call type 的模型与 backend 分配。
- `providers/`：已实现的 ProviderProfile 模板，收拢能力、请求映射、usage、错误分类与价格。
- `profiles/`：混合后端与 all-local 运行模式。
- `policies/`：限流、重试、fallback、防护与预算。
- `workloads/`：长度分布、并发、共享前缀和 judgment workload。

Roadmap 中的 call types：event filter second stage、input classifier、output check、Router decision、Router reply、Worker steps、RAG embeddings。未决 provider 和模型不填虚构默认值。
