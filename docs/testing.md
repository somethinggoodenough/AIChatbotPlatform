# 测试与验收规范

各阶段清单位于 [tests/acceptance](../tests/acceptance/README.md)，来自 Roadmap。当前已有 Phase 0–5 的离线测试与实验；真实系统验收和 mock 证据分别记录，见 [离线验收报告](reports/offline-acceptance.md)。

从仓库根目录运行全部测试：

```sh
uv sync --frozen
uv run python scripts/acceptance/run_all.py
```

统一入口发现所有 `test_*.py`，只允许 loopback 网络连接。真实环境要求在 `tests/acceptance/test_live_requirements.py` 等测试中带原因 skip；通过数不包含这些 skip。测试不读取 `.env` 来启动付费调用。

## 分层

- unit：配置、usage、schema 等确定性逻辑；benchmark helper 测试只连接本机 mock。
- contract：不同 backend 的流、取消、usage 和错误语义一致。
- integration：模型服务、数据库和运行时组件协作。
- e2e：完整渠道行为的目标分层；当前 fake-channel 路径在 `integration/test_agent_runtime.py`，真实 Discord 仍跳过。
- fault_injection：显式覆盖持久化边界、provider 故障和进程停止。
- acceptance：汇总阶段标准、运行命令、结果与未完成项。

按文件调试可以运行 `uv run python -m unittest discover -s tests/integration -p 'test_agent_runtime.py' -v`；提交前仍应执行统一入口。模型服务 mock 能配置延迟、prefix cache、429/5xx、断连和 keep-alive 挂起，fake channel 检查回复和节流编辑。

## 必须保留的行为约束

Router 没有执行工具的代码路径；Worker 不直接向 Discord 发消息；线程 follow-up 命中订阅后不重复做 Router 决策。验证断连时的取消传播及 API 上游流关闭，不能只验证客户端停止读取。

恢复测试对新建 Worker 子进程执行真实 SIGKILL，分别覆盖 model/tool 的调用前、调用后但 journal 写入前、journal 写入后三个边界；使用租约、fencing token 和幂等效果日志检查恢复。mock 的幂等 receipts 不代表真实 provider 保证；未知外部调用结果保留为 uncertain。故障指标必须包含失败和取消请求。

## 阶段证据

每个阶段记录代码版本、机器 / 模型 / provider、配置、seed、实际运行命令及原始结果索引。性能阈值若 Roadmap 未给出，应在执行前说明测量口径和接受标准，不伪造既定目标。

基线、恢复、路由、防护与消融命令见 [运行手册](runbook.md)，结果写入 `artifacts/benchmarks/`。mock timing、synthetic token 和固定 judge 标签只说明实现行为；真实模型输出正确率、安全性与委派能力仍需独立验收。

每个阶段提交前，完整离线套件必须通过；不应为消除 skip 而虚构硬件、收费调用或 Discord 连接。日志、trace 和结果摘要不保存消息、reasoning 或密钥。业务数据库中的消息与 journal 是应用状态，需要单独保护。
