# 部署

`sglang/compose.yaml` 与 `sglang/launch.py` 已提供 Phase 0 单 GPU 实验服务的启动配置；需要在 NVIDIA GPU 所在的 Linux / WSL2 主机运行。平台服务和观测组件的独立部署仍待实现。

- `sglang/`：Linux / WSL2 GPU 服务启动与模型参数。
- `app/`：平台进程、配置注入及 SQLite 持久卷。
- `observability/`：Prometheus 与 OpenTelemetry 收集配置。

Phase 4 验收前 Discord 仅连接私有测试服务器。
