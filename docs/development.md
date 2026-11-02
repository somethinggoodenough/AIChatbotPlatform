# 开发起步

当前已有 ProviderProfile 基础实现和四个 serving spike（Python 3.11 标准库，无额外依赖），以及 SGLang 容器启动配置；尚无平台服务或数据库 schema。用法见 [ProviderProfile](provider-profiles.md) 与 [SGLang 部署](../deploy/sglang/README.md)。

从仓库根目录执行离线测试：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests/unit -p 'test_*.py' -v
```

1. 阅读 Roadmap 的 Phase 0 和对应验收清单，先明确 backend 的请求、流、取消与 usage 契约。
2. 使用 Python 3.11+；本地 SGLang 推理主机使用 Linux 或 WSL2，RTX 4060 8 GB。客户端开发和 mock 验证可与 GPU 主机分离。
3. 选定依赖后补充打包配置与锁定文件，再实现 SGLang 和一个外部 provider。
4. 同步加入契约测试、基准框架和实际可执行的复现入口，更新 README。

源码放 `src/ai_serving/`；可提交配置放 `configs/`；密钥不入库。日志、数据库、缓存、模型权重和生成结果不进入源码目录。

API provider、具体模型与 repo 正式名称仍待选择。当前 `ai_serving` 只是内部包名，未替用户决定正式项目名称。
