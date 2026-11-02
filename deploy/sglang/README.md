# SGLang 部署

用于 Phase 0 的单卡实验服务。运行在 **RTX 4060 所在的 Linux / WSL2 主机**；当前 Mac 只作为 HTTP 客户端，不能通过 Docker 获得另一台机器的 GPU。

首次运行需要 NVIDIA 驱动、Docker Engine / Compose v2，以及已配置好的 NVIDIA Container Toolkit。先在 GPU 主机运行 `nvidia-smi`；驱动必须满足所选镜像的 CUDA 要求。尚未在用户的 4060 上完成兼容性或显存实测。

## 启动

将项目放到 GPU 主机，进入仓库根目录，准备该机器的 `.env`（可从 `.env.example` 复制；不要覆盖已有密钥）。实验模型默认 `Qwen/Qwen2.5-0.5B-Instruct`，是为验证服务行为选择的小模型，不代表最终 Router 选型。

```sh
docker compose --env-file .env -f deploy/sglang/compose.yaml up -d --wait --wait-timeout 900
```

首次启动会下载镜像和权重。默认固定 `lmsysorg/sglang:v0.5.19-cu129`；可通过 `SGLANG_IMAGE` 换成与驱动匹配的版本，换版本后重跑全部探针。此 CUDA 12 镜像线依据 [SGLang 安装说明](https://docs.sglang.io/docs/get-started/install) 选定，不使用浮动 latest。最终复现还应记录镜像 digest：

```sh
docker image inspect lmsysorg/sglang:v0.5.19-cu129 --format '{{json .RepoDigests}}'
docker compose --env-file .env -f deploy/sglang/compose.yaml logs --tail 100 sglang
```

启动参数：FP16、context 4096、静态显存比例 0.65、最多 4 个 running requests；关闭 CUDA graph 以降低初次实验的内存压力。启用 `--enable-cache-report`、`--enable-metrics`、`--decode-log-interval 1`、`--stream-interval 1`。这些是功能探针配置，不是性能最优配置；性能基准后续单独调参。

## 从 Mac 连接

容器只绑定 GPU 主机的 `127.0.0.1:30000`。在 Mac 终端建立隧道，替换实际 SSH 用户和主机：

```sh
ssh -N -L 30000:127.0.0.1:30000 user@gpu-host
```

Mac 的 `.env` 保持 `SGLANG_BASE_URL=http://127.0.0.1:30000/v1`，`SGLANG_MODEL` 与 GPU 主机一致。若已启用 SGLang API key，两端填写相同 key。GPU 主机 SSH 地址尚待提供。

```sh
curl --fail http://127.0.0.1:30000/health
```

然后按 [四个 spike 的说明](../../experiments/phase_0_serving_baseline/spikes/README.md) 执行。

## 日志与停止

在 GPU 主机保存本轮日志，保留 `#cached-token` 和 running request 的辅助证据：

```sh
mkdir -p artifacts/spikes
docker compose --env-file .env -f deploy/sglang/compose.yaml logs --no-color --timestamps sglang > artifacts/spikes/sglang-server.log
rg '#cached-token|#running-req|Abort|disconnected' artifacts/spikes/sglang-server.log
```

不要把所有日期的命中日志混成一次实验结论；用 JSON 报告中的 UTC 时间、请求 id 和串行运行窗口对齐。原始指标快照和 cached token 数已由脚本写入 JSON。

```sh
docker compose --env-file .env -f deploy/sglang/compose.yaml down
```

`down` 保留模型缓存卷。断连测试若发现残留请求，不会偷偷调用 abort 接口使其通过；报告失败后可停止实验容器。
