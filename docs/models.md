# 模型接入

支持 DeepSeek、Kimi、GLM、OpenAI（ChatGPT 系列 API）及兼容 Chat Completions 的服务。每个部署使用一组服务端模型配置；修改被 Git 忽略的 `.env` 后，重启 API 和 Worker。密钥不进入浏览器。

## 官方接口

填写以下配置，将表中的服务商、地址和模型 ID 对应替换：

```dotenv
BUILDMEDIC_MODEL_PROVIDER=kimi
BUILDMEDIC_MODEL_ENDPOINT_MODE=official
BUILDMEDIC_MODEL_BASE_URL=https://api.moonshot.cn/v1
BUILDMEDIC_MODEL_API_KEY=your-server-side-key
BUILDMEDIC_MODEL_ID=kimi-k2.6
BUILDMEDIC_MODEL_THINKING=disabled
BUILDMEDIC_MODEL_TOKEN_PARAMETER=auto
BUILDMEDIC_MODEL_REASONING_EFFORT=
```

| 服务商 | `MODEL_PROVIDER` | `MODEL_BASE_URL` | 模型 ID 示例 |
| --- | --- | --- | --- |
| DeepSeek | `deepseek` | `https://api.deepseek.com` | `deepseek-flash` |
| Kimi | `kimi` | `https://api.moonshot.cn/v1` | `kimi-k2.6` |
| Kimi 国际站 | `kimi` | `https://api.moonshot.ai/v1` | `kimi-k2.6` |
| 智谱 GLM | `glm` | `https://open.bigmodel.cn/api/paas/v4` | `glm-5.3` |
| Z.AI | `glm` | `https://api.z.ai/api/paas/v4` | `glm-4.7` |
| OpenAI | `openai` | `https://api.openai.com/v1` | `gpt-4.1` |

表中模型仅作配置示例，不自动选择或替换；账户权限、具体模型支持的参数和可用 ID 以服务商为准。`chatgpt`、`moonshot`、`zhipu` 分别是 `openai`、`kimi`、`glm` 的别名。OpenAI 接入需要 API 密钥。

官方模式校验服务商对应的 HTTPS 主机及 API 根路径，地址不匹配时在发送密钥前拒绝请求。DeepSeek 也接受 `/v1` 根路径。

## 中转站

将地址类型切换为 `relay`，填写中转站给出的 API 根地址、密钥和模型 ID，保留实际模型服务商以启用其参数适配：

```dotenv
BUILDMEDIC_MODEL_PROVIDER=kimi
BUILDMEDIC_MODEL_ENDPOINT_MODE=relay
BUILDMEDIC_MODEL_BASE_URL=https://your-relay.example/v1
BUILDMEDIC_MODEL_API_KEY=your-relay-key
BUILDMEDIC_MODEL_ID=kimi-k2.6
BUILDMEDIC_MODEL_THINKING=disabled
BUILDMEDIC_MODEL_TOKEN_PARAMETER=auto
BUILDMEDIC_MODEL_REASONING_EFFORT=
```

DeepSeek、GLM、OpenAI 的中转方式相同，只需替换服务商及模型 ID。中转站使用命名空间时，例如 `moonshot/kimi-k2.6` 或 `zhipu/glm-5.3`，保留其提供的完整模型 ID。

中转站地址允许任意 HTTPS 根地址，以及仅用于本机调试的 HTTP 地址。不跟随重定向；URL 不允许内嵌账户、密码或查询参数。密钥只发送给配置的中转站，由其处理下游调用。

`MODEL_BASE_URL` 只填写根地址，程序会追加 `/chat/completions`；不要填写完整的补全接口路径。

### 中转协议差异

- `MODEL_TOKEN_PARAMETER=auto`：OpenAI 使用 `max_completion_tokens`，其他服务使用 `max_tokens`。若中转站要求另一种字段，可显式选择 `max_tokens` 或 `max_completion_tokens`，仍使用同一输出预算。
- 若中转站要求统一的普通 OpenAI 兼容格式、不接受服务商专属 `thinking` 参数，可使用 `MODEL_PROVIDER=compatible`。该模式不追加思考字段，默认发送 `max_tokens`；关闭思考需由中转站自行保证。
- `MODEL_REASONING_EFFORT` 默认留空，不发送此字段；确需使用时填写具体模型支持的值，例如 `none`、`low`、`medium`、`high`。服务商不支持的值会返回调用错误，不会悄悄改用其他模型或重试付费请求。
- 所选模型和中转站须支持 Chat Completions 的 JSON 对象输出；工具诊断还须支持原生 `tools` / `tool_calls`。不在此入口调用 Responses 或 Anthropic Messages 协议。

## 思考与工具诊断

标准诊断与 Harness² 的 JSON 调用共用上述适配。工具诊断继续使用原生 Function Calling，最终补丁仍须通过独立检查。

DeepSeek、Kimi K2.5 / K2.6、GLM 4.5 及以上模型会按配置发送 `thinking.type`。较早的 Kimi / GLM 模型不追加此字段；不强制设置采样参数。Kimi K3、K2.7 Code、K2 Thinking 始终思考，须设 `MODEL_THINKING=enabled`，不发送可切换的思考字段。

平台不保存或回传 `reasoning_content`。因此命名服务商的工具模式要求关闭思考；上述始终思考的 Kimi 模型可用于 JSON 诊断，不开放工具模式。OpenAI 的推理强度与工具兼容性按具体模型文档配置，不保证所有模型支持所有参数。

## 检查连接

```sh
# 不进行付费生成
uv run --extra adaptive python scripts/check_model.py

# 明确进行一次生成，输出上限 512 Tokens
uv run --extra adaptive python scripts/check_model.py --generate
```

创建诊断页面的“检查模型连接”也不产生付费生成：

- 提供 `/models` 的接口检查鉴权及所选 ID，返回 `available`；这仍不能证明余额充足或实际生成成功。
- GLM 官方模式没有本项目使用的模型列表探测，返回 `unverified`，不声称密钥有效。
- 中转站会尝试 `/models`；仅在该路径返回 404 / 405 / 501 时提示 `unverified`，不会偷偷调用生成接口。鉴权失败、限流和重定向继续报错。

CLI 对未验证状态返回退出码 `2`；检查失败返回 `1`；模型列表验证或显式生成通过返回 `0`。真实修复能力还需实际案例、Runner 和独立回归验收。

## 验证范围与参考

自动化测试使用明确的 HTTP 模型替身，覆盖官方地址与中转站、JSON / 工具调用、用量、错误处理、思考内容不外流及原生 Harness² 调度。本轮 484 项后端、36 项浏览器检查通过，另通过静态检查、生产构建和生成合同一致性；60 项显式 Docker 检查默认跳过。真实服务验收仍仅有现有 DeepSeek 记录；没有用其密钥向其他服务商或中转站发请求，也没有宣称完成 Kimi、GLM、OpenAI 的真实账户验收。

官方协议核对日期：2026-10-29。[OpenAI Chat Completions](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create)、[Kimi Chat Completions](https://platform.kimi.com/docs/api/chat)、[Kimi 思考模式](https://platform.kimi.com/docs/guide/use-thinking-models)、[Kimi 国际站兼容说明](https://platform.kimi.ai/docs/guide/migrating-from-openai-to-kimi)、[GLM API](https://docs.bigmodel.cn/cn/api/introduction)、[GLM 对话补全](https://docs.bigmodel.cn/api-reference/模型-api/对话补全)、[Z.AI API](https://docs.z.ai/api-reference/llm/chat-completion)。DeepSeek 现有配置和验收见 [DeepSeek 说明](deepseek.md)。
