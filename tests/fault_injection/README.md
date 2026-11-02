# 故障注入

Phase 2：在调用前、调用后但 journal 写入前、journal 写入后注入 crash，验证工具幂等日志。Phase 3：本地副本终止、API timeout / 429 / 5xx 与连接中断。Phase 4：分类器停止后的回退行为。
