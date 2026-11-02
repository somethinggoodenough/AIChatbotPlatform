# 决策记录

Roadmap 已确定：SGLang 首个本地后端、外部 API 同级、自己实现网关、Router / Worker 分层、SQLite WAL 优先、Discord 首发、Python asyncio 当前默认。

仍待决定：

- 主 / 备用 API provider。
- Router / guardrail 模型和 embedding 模型的 8 GB 显存安排。
- Discord 图片输入是否进入范围。
- GPU 主机 Linux 或 WSL2。
- Python 全栈或 Go 网关（当前先按 Python 目录布局）。
- 正式 repo 名称。
- 是否为朋友已有 bot 贡献一个有边界的模块。

新增选择使用 [ADR 模板](../templates/decision.md)，记录原因、对照、影响与状态，不把目录名视为已批准的产品决策。
