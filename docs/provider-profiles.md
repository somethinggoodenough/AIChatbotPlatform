# ProviderProfile 与 API 差异

当前已实现 `.env` / TOML 加载、ProviderProfile、Chat Completions 请求编码、usage 归一化、流式 usage 汇总、错误分类和 token 费用估算。全部使用 Python 3.11 标准库。

已为 [Phase 0 spikes](../experiments/phase_0_serving_baseline/spikes/README.md) 编写实验专用 HTTP/SSE 客户端；生产后端的响应内容归一化、取消传播、重试与 fallback 仍待实现。两个初始 profile 均开启 `request.stream_usage = true`，由 spike 验证实际兼容性。JSON schema 请求编码不等于返回值 schema 校验；spike 额外校验其固定 schema。现有 profile 仍是初始配置/模板，不是已验证的供应商兼容性声明。

## 使用与切换

本地 `.env` 已创建；`.env.example` 是可提交的样例，`.env` 已被 `.gitignore` 忽略。先填写当前 profile 所需的 URL、真实模型 id 和密钥；本地模型 id 不能从 GPU 型号推断。

```dotenv
# 使用本地 SGLang
PROVIDER_PROFILE=configs/providers/local_sglang.toml
SGLANG_BASE_URL=http://127.0.0.1:30000/v1
SGLANG_MODEL=实际部署的模型ID
```

切换到 DeepSeek 时，将 `PROVIDER_PROFILE` 改为 `configs/providers/deepseek.toml`。当前官方 OpenAI-format 地址为 `https://api.deepseek.com`，当前默认实验模型为 `deepseek-flash`；密钥使用 `DEEPSEEK_API_KEY`。profile 的 endpoint 是 `/chat/completions`，最终 URL 为 `https://api.deepseek.com/chat/completions`。

OpenAI、Gemini、Claude/Anthropic 和 Grok/xAI 将分别使用 `OPENAI_*`、`GEMINI_*`、`ANTHROPIC_*`、`XAI_*`。每个 provider 单独建 profile；同一 provider 若模型能力或计费不同，也应拆成独立 profile。gateway 以后按 call type 引用 profile 名称，业务代码不读取 provider 专属环境变量。

业务调用保持一致：

```python
from pathlib import Path
from ai_serving.config.providers import load_active_profile

profile = load_active_profile(Path.cwd())  # 从仓库根目录运行
body = profile.build_request(
    [{"role": "user", "content": "你好"}],
    stream=True,
    max_output_tokens=128,
)
# 后续 transport 使用 profile.url / profile.request_headers() / body 发请求。
# 不要记录 request_headers()，其中含密钥。
```

进程环境变量覆盖 `.env`；加载器不修改 `os.environ`，不执行 shell、不插值 `$VARIABLE`。profile 的能力和价格应随代码版本保存。不同模型建议各建独立 TOML，避免只改 model 环境变量后继续使用旧模型的单价与能力。

`load_active_profile` 当前提供单 profile 入口。Roadmap 的异构路由后续由 gateway 按 call type 选择多个 profile，使用 `load_profile(path, env)` 加载；`configs/profiles/` 的 hybrid / all-local 运行模式和这里的 provider profiles 是不同层次。RAG embeddings 需要单独的 embedding 契约和编码器，不应经过当前 chat 请求编码器。

## 哪些差异放在哪里

| 差异 | 处理位置 |
| --- | --- |
| 能力、字段名、静态 headers、错误码、usage 字段、单价 | TOML profile；同协议内切换无需业务代码分支 |
| 原生 Messages / Responses 等不同消息结构与流事件协议 | 单独实现并测试 transport/协议适配器，再由 profile 选择 |
| 超时预算、退避、fallback 顺序、用户预算 | gateway 策略，使用 profile 的分类结果 |
| 任务恢复、side effect 幂等、是否允许重复执行 | runtime/journal，不由 provider 配置决定 |

配置不加载任意 Python 类，不执行脚本，也不允许随意覆盖 canonical request 字段。当前只实现 `chat_completions` 编码器，配置其他协议会明确报错；新增协议不可能仅靠更换 URL 获得兼容性。

## Capability：要求不满足就失败

JSON mode 和 JSON schema enforcement 是两种能力；`json_schema` 与 `strict_json_schema` 也分别声明。请求 schema、strict、reasoning、tools 或 temperature 而 profile 未开启对应能力时，抛出 `UnsupportedCapability`，不静默删除参数或退回自由文本。

reasoning 是独立控制通道，不拼进用户消息。例如经该模型接口验证后可配置：

```toml
[capabilities]
reasoning = true

[request]
reasoning_field = "thinking.effort"

[request.reasoning_values]
low = "small"
high = "large"
```

这只是字段映射示例，不代表任何厂商真实枚举。映射也支持 `reasoning_effort` 这样的平面字段。自动 reasoning、budget tokens、reasoning 内容回传、签名保留、tools+reasoning 或 schema+streaming 的组合限制，需要对应协议适配器和契约测试；独立布尔开关不能证明所有组合都支持。

## Usage 和价格口径

字段路径相对于 **usage 对象**，由 transport 从完整响应或流事件中提取后传入。统一 `input_tokens` 包含 cache read/write；`reasoning_tokens` 是 output 的子集，不在输出价格外重复累加。

```python
usage = profile.usage_normalizer.normalize(response.get("usage"))
estimated_cost = profile.prices.estimate(usage)  # Decimal 或 None

accumulator = profile.usage_accumulator()      # 每个响应新建一个
accumulator.update(event_usage)               # transport 提取的 usage，可为 None
usage = accumulator.normalized(profile.usage_normalizer)
```

`stream_mode = "cumulative"` 合并部分快照并替换已出现的计数，避免重复相加；`delta` 仅用于确认返回增量的接口，transport 必须保证事件不重复处理。不要对不同请求共享 accumulator。

缺失字段保留 `None`。空缓存字段路径 `""` 表示已确认没有该计费桶，按 0 处理；不要为获得数字而把未知字段改成空路径。`input_includes_cache = false` 会将独立 cache read/write 加回 input；任一所需字段未知时总数仍未知。

价格是每百万 token 的字符串，加载后转为 Decimal。输入普通、缓存读、缓存写和输出分桶计算；若未知的 cache 划分影响费用，返回 `None`。价格表需填写来源和生效日期，且只适用于一个模型/档位。长上下文分档、batch 折扣、不同缓存 TTL、多模态单位、搜索调用费等不在当前固定 token 表的表达范围；这些场景需扩展计价策略后才能作为预算依据。

取消、截断或中途断流时可能收不到最终 usage。已收到的累计值只能视为部分测量，不是完整账单；transport 后续必须记录完成状态，预算层应保留估算/预留费用并对账，不能把 `None` 当零。关闭上游连接也不保证已发生的费用可以撤销。

## 错误分类与重试

```python
error = profile.error_classifier.classify(
    status=429,
    body={"error": {"code": "insufficient_quota"}},
    headers={"Retry-After": "2"},
)
# deepseek profile 中，HTTP 402 会归为 quota，retry_candidate=False
```

优先读取 profile 指定的错误 code/type，再使用 HTTP status；自定义 status 合并通用默认值。无可识别代码的 5xx 归为 transient，未知错误默认不重试。`Retry-After` 支持秒数和 HTTP 日期；非法值保留未知。HTTP 200 后的 SSE error 事件也要经过分类，不能只检查响应头状态。

`retry_candidate` 只是暂时性错误标记。真正重试前，gateway 必须判断是否已向用户输出 token、请求是否可能执行过、幂等保证、重试预算和剩余 deadline。timeout/connection 不证明请求未执行；取消不是重试候选。已经输出的流不能直接拼接另一个 provider 的新回答。

## 新 provider 的接入验收

1. 锁定 endpoint/API 版本、model、部署参数和计费档位，复制一份独立 profile。
2. 根据官方文档配置，保存去除敏感信息的 fixture；未验证的能力先关闭。
3. 探针覆盖普通/流式请求、schema 与拒答/截断、reasoning、tool-call 参数增量和所需能力组合。
4. 检查最后 usage 帧、空 choices、finish reason、HTTP 200 流内错误、429 额度/限流区别、5xx、断流和取消。
5. 对照官方 usage/账单检查缓存和 reasoning 计费；更新价格来源、日期及兼容性证据。供应商、模型或接口升级后重新运行相关探针。

当前离线测试只验证本项目的编码和归一化行为，不替代这些真实接入验收：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests/unit -p 'test_*.py' -v
```

## 核对过的协议资料

- [SGLang structured outputs](https://docs.sglang.io/docs/advanced_features/structured_outputs)：schema 约束及 grammar backend；实际部署仍需验证。
- [DeepSeek context caching](https://api-docs.deepseek.com/guides/kv_cache/)：usage 中缓存 hit/miss 的专用字段，说明字段映射不能只假设一种格式。
- [Claude prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)：独立的 input/cache read/cache write 计数口径。
- [Claude API errors](https://platform.claude.com/docs/en/api/errors)：错误类型及流开始后仍可能发生的错误。

以上资料用于确定适配边界；没有据此填写未经选型确认的外部模型价格，也未宣称支持这些厂商的全部协议。
