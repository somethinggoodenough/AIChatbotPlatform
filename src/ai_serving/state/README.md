# 状态存储

Phase 1：任务、线程、订阅和 session 的存储接口；Phase 2 增加 journal、checkpoint、heartbeat、补偿记录。实现不得依赖 Router 的进程内唯一状态。
