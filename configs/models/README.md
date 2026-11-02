# 模型分配

按 call type 绑定 backend / model；保留本地 embedding 与模型的显存预算。

provider 差异和模型能力定义在 [providers/](../providers/README.md)，不应散落在 call type 路由里。未来的模型分配配置引用 profile；当前可通过根目录 `.env` 的 `PROVIDER_PROFILE` 选择单个 profile，按 call type 的多模型路由尚未实现。
