# 工具

Phase 1：Worker 专用工具接口及 toy tools。Phase 2：read_only / idempotent / side_effecting 分类；有副作用的工具必须接收 task_id:step_index 幂等键，并记录补偿操作。
