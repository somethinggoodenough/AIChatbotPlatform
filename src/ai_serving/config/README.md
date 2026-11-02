# 配置加载

`providers.py` 已实现 `.env` 读取、严格 TOML ProviderProfile 加载与当前 profile 选择；进程环境覆盖 `.env`，不会执行 shell 或修改进程环境。

call type 分配、hybrid / all-local 模式和运行策略的加载仍待实现。密钥从环境或外部密钥系统注入，不写入版本库。
