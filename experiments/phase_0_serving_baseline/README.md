# Phase 0 实验

SGLang 功能探针（prefix cache / abort / JSON schema）、local / API 统一调用和延迟基线；输出 TTFT、ITL、吞吐、Router-sized structured latency，以及 gateway / provider 耗时拆分。

四个功能探针已实现，见 [spikes/](spikes/README.md)。启动配置见 [SGLang 部署](../../deploy/sglang/README.md)。真实 GPU / API 验证尚未通过；状态与阻塞条件见 [执行报告](../../docs/reports/phase-0-spikes.md)。
