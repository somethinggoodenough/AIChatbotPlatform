# 测试与验收规范

先写验收条件，再实现代码。各阶段清单位于 `tests/acceptance/`，从 Roadmap 提取；当前所有条目均未执行。

## 分层

- unit：确定性逻辑，默认不访问网络或 GPU。
- contract：不同 backend 的流、取消、usage 和错误语义一致。
- integration：模型服务、数据库和运行时组件协作。
- e2e：从渠道消息到用户回复的完整行为，包含 all-local。
- fault_injection：显式覆盖持久化边界、provider 故障和进程停止。
- acceptance：汇总阶段标准、运行命令、结果与未完成项。

实现测试框架时区分 mock、真实 GPU、真实 provider 和 Discord 环境；缺少凭据或硬件时明确跳过，不能记录为验收通过。默认本地快速检查不应意外发起计费调用。

## 必须保留的行为约束

Router 没有执行工具的代码路径；Worker 不直接向 Discord 发消息；线程 follow-up 命中订阅后不重复做 Router 决策。验证断连时的取消传播及 API 上游流关闭，不能只验证客户端停止读取。

恢复测试逐一覆盖调用前、调用后但 journal 写入前、journal 写入后的 crash。用幂等日志检查工具效果，报告 provider 不具备幂等能力时的限制。故障指标包括失败请求，不能只统计存活任务。

## 阶段证据

每个阶段记录代码版本、机器 / 模型 / provider、配置、seed、实际运行命令及原始结果索引。性能阈值若 Roadmap 未给出，应在执行前说明测量口径和接受标准，不伪造既定目标。

完成后更新阶段清单与 docs/reports；原始输出放 artifacts，长期证据附可访问的归档位置。目前使用 unittest 运行 unit 与 ModelBackend contract tests，命令见根 README；阶段验收入口仍待实现。
