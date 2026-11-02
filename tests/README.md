# 测试

目录按验证范围组织。`unit/test_provider_profiles.py` 覆盖配置、能力检查、请求映射、usage、价格和错误分类；`unit/test_serving_spikes.py` 用本机 HTTP 假服务覆盖四个探针及失败分支，不访问外部 API。阶段验收尚未完成。

```sh
PYTHONPATH=src python3 -m unittest discover -s tests/unit -p 'test_*.py' -v
```

| 目录 | 验证范围 |
| --- | --- |
| unit/ | 纯逻辑、schema、配置、限流和状态转换 |
| contract/ | 不同 backend / adapter / store 的一致语义 |
| integration/ | 后端、状态存储和服务组件之间的协作 |
| e2e/ | Discord → Router → Worker → models 的完整路径 |
| fault_injection/ | crash、超时、429、5xx、断连、挂起和循环 |
| acceptance/ | Roadmap 各阶段退出条件和证据 |
| fixtures/ | 脱敏或合成消息、工具、文档及标签 |

执行范围、隔离方式及证据要求见 [测试规范](../docs/testing.md)。
