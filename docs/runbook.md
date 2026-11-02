# 本地复现与 GPU 恢复后的执行顺序

所有命令从项目根目录运行。日常默认模式只访问 loopback mock，不读取 `.env` 来调用 provider，不连接 Discord。

## 现在：全离线验证

```sh
uv sync --frozen
uv run python scripts/acceptance/run_all.py
uv run python -m benchmarks.harness.baseline
uv run python -m ai_serving.runtime.demo
uv run python -m benchmarks.harness.recovery
uv run python -m benchmarks.harness.routing
uv run python -m benchmarks.harness.guards
uv run python -m benchmarks.harness.ablation --runs 5
```

每个 benchmark 会启动并关闭自己的 mock server。结果放在 `artifacts/benchmarks/`，不提交运行数据库或原始消息。测试中的 SIGKILL 只作用于测试新建的 Worker 子进程。

启动可手动调用的本机服务：

```sh
uv run python -m ai_serving.api --mock --port 8080
```

另一个终端：

```sh
curl --fail http://127.0.0.1:8080/health
curl --fail http://127.0.0.1:8080/metrics
curl --fail --no-buffer http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"configured","messages":[{"role":"user","content":"hello"}],"stream":true,"stream_options":{"include_usage":true}}'
```

## GPU 恢复后

先把本轮代码提交同步到 Linux/WSL2 GPU 主机，按 [SGLang 部署说明](../deploy/sglang/README.md) 准备 NVIDIA 驱动、Docker 和 NVIDIA Container Toolkit。GPU 主机与客户端 `.env` 的模型名称应一致；不需要填付费 API key。不要覆盖已有 `.env`。

1. 在 GPU 主机检查显卡并启动 SGLang：

   ```sh
   nvidia-smi
   docker compose --env-file .env -f deploy/sglang/compose.yaml up -d --wait --wait-timeout 900
   docker compose --env-file .env -f deploy/sglang/compose.yaml logs --tail 100 sglang
   curl --fail http://127.0.0.1:30000/health
   ```

2. 如果从 Mac 测试，在独立终端保持 SSH 隧道运行，替换实际 SSH 地址：

   ```sh
   ssh -N -L 30000:127.0.0.1:30000 user@gpu-host
   ```

   客户端 `.env` 使用 `SGLANG_BASE_URL=http://127.0.0.1:30000/v1`。在 GPU 主机直接测则不需要隧道。

3. 在测试客户端安装依赖并跑离线回归，先确认应用代码正常：

   ```sh
   uv sync --frozen
   uv run python scripts/acceptance/run_all.py
   ```

4. 按顺序验证真实 SGLang 的 streaming+usage、schema、prefix cache、断开后的 running request：

   ```sh
   uv run python experiments/phase_0_serving_baseline/spikes/01_streaming.py --target local
   uv run python experiments/phase_0_serving_baseline/spikes/02_structured_output.py
   uv run python experiments/phase_0_serving_baseline/spikes/03_prefix_cache.py
   uv run python experiments/phase_0_serving_baseline/spikes/04_disconnect.py
   ```

   最后一项需要独占实例，避免其他请求干扰 running/queued 计数。不要使用 `--target both`，它会尝试外部 API。

5. 验证新增 Backend 的流式与聚合结果，然后测真实服务基线：

   ```sh
   uv run python scripts/acceptance/backend_smoke.py
   uv run python -m benchmarks.harness.baseline --local --concurrency 1 2 4 --requests 24
   ```

   基线写入 `artifacts/benchmarks/sglang/`，与 mock 数据分开。真实服务的 chunk 不一定对应一个 token，因此时间间隔按 inter-chunk 解读。

6. 接入平台 API，并在另一个终端重复前面的 curl 请求：

   ```sh
   uv run python -m ai_serving.api --local --port 8080
   ```

真实 provider、真实 Discord、SGLang Gateway `cache_aware` 对比、真实模型 red-team judge 和模型依赖的消融结果，仍需分别验证。恢复 GPU 不等于这些验收自动通过。只有保存真实报告后才更新 Roadmap 对应验收项。

## 边界

lease + fencing 可以拒绝过期 Worker 对我们的状态与示例 effect sink 的写入；第三方工具必须在自己的副作用落点实现对应幂等/隔离机制。未知 provider 调用结果保留为 uncertain。真实 Discord 的部分消息交付也可能未知，不能承诺端到端 exactly-once。

fake guard/judge 只验证系统行为；未经真实安全验收不要把 bot 开到公共服务器。输出缓冲和慢 classifier 都可能增加首字延迟。付费成本上限需要已验证的价格和最坏成本边界；未配置时防护层默认拒绝付费或价格未知的 route。
