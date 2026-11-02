# 契约测试

`test_model_backend.py` 使用真实本地 TCP/SSE 服务验证各 ModelBackend 的 streaming、终态 usage、health、provider 隔离和 structured output capability。取消测试只有在服务端观察到客户端关闭连接后才通过，覆盖 task cancellation 与 generator `aclose()`。

non-streaming、后续 ChannelAdapter 和 state store 契约仍待实现。
