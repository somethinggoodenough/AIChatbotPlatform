# 指标与追踪

Phase 0：TTFT、ITL、token、API 成本和后端错误的 Prometheus 指标。Phase 1：请求级 OpenTelemetry traces。后续补充恢复、缓存、fallback 与防护开销。

`Metrics` 是进程内实验 registry，保留当前进程的观测值以计算 p50/p95/p99；不是跨进程账本或长期指标数据库。`/metrics` 输出 Prometheus counter/summary。生产长期运行应换成有界直方图或外部指标系统。

TTFT 计到第一段 text 或 reasoning；`model_inter_chunk_seconds` 计客户端观察到的相邻内容块间隔，不能假称真正的 token 间隔，因为 SSE 块可能包含多个 token。`model_provider_seconds` 累加等待上游迭代的时间，`gateway_overhead_seconds` 记录本地路由/记账处理时间；下游消费暂停不计入这两项。token 缺失和价格未知有独立计数，不默认为免费。标签仅含 backend、call type、状态、token 种类和币种，不包含消息或密钥。
