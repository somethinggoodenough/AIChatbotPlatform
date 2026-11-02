# Provider profiles

每份 TOML 描述一个 **provider + endpoint/API 版本 + model + 部署/计费档位**。同一厂商不同模型可以有不同能力；换模型时必须重新核对能力和价格。

- [local_sglang.toml](local_sglang.toml)：本地 SGLang 的初始配置，待真实部署探针验证。
- [deepseek.toml](deepseek.toml)：DeepSeek OpenAI-format Chat Completions profile。
- [openai.toml](openai.toml)：OpenAI Chat Completions profile，具体模型待配置。
- [gemini.toml](gemini.toml)：Gemini OpenAI-compatibility profile，具体模型待配置。

`.env` 保存 endpoint、模型名、密钥及当前 profile 路径。TOML 保存可审阅的能力、请求字段映射、usage normalizer、error classifier 和价格；不要把密钥填进 TOML。

| 表 | 内容 |
| --- | --- |
| profile | 名称、协议、URL/model 的环境变量名、endpoint、鉴权方式、静态版本 headers |
| capabilities | streaming、JSON schema、strict schema、reasoning、tools、temperature |
| request | token 上限字段、stream usage 开关、reasoning 字段路径及值映射 |
| usage | token 字段路径、input 是否包含缓存、流式累计或增量口径 |
| errors | provider code/type 和 HTTP status 到统一错误种类的映射 |
| pricing | 每百万 token 的 Decimal 单价、币种、生效日期与来源 |

字段拼写错误、未知协议、不完整的 reasoning 映射、无来源/日期的价格会在加载时失败。价格留空表示未知；本地零价仅指 API 调用费，不包括硬件、电费与托管成本。

新增 provider 时采用独立文件和独立环境变量 namespace：

| Provider | Profile | 环境变量前缀 |
| --- | --- | --- |
| DeepSeek | `deepseek.toml` | `DEEPSEEK_` |
| OpenAI | `openai.toml` | `OPENAI_` |
| Gemini | `gemini.toml` | `GEMINI_` |
| Claude / Anthropic | 待实现 | `ANTHROPIC_` |
| Grok / xAI | 待实现 | `XAI_` |

这些 provider 的协议并不完全相同。只有完成请求、流事件、usage、错误和能力探针后才加入对应 TOML；不能复制 DeepSeek profile 后只换 URL。

完整使用方法与 API 差异处理约定见 [ProviderProfile 说明](../../docs/provider-profiles.md)。
