# DeepSeek 官方 API 接入

服务端使用 DeepSeek 的 Chat Completions 兼容接口。官方文档核对日期为 2026-10-28；当前文档给出的模型 ID 为 `deepseek-flash` 与 `deepseek-v4-pro`。本地开发选用 `deepseek-flash`，实际可用性以账户 `/models` 返回值为准。

仅在被 Git 忽略的 `.env` 中填写密钥：

```dotenv
BUILDMEDIC_MODEL_PROVIDER=deepseek
BUILDMEDIC_MODEL_BASE_URL=https://api.deepseek.com
BUILDMEDIC_MODEL_API_KEY=replace-locally
BUILDMEDIC_MODEL_ID=deepseek-flash
BUILDMEDIC_MODEL_THINKING=disabled
BUILDMEDIC_MAX_OUTPUT_TOKENS=4096
BUILDMEDIC_MODEL_TIMEOUT_SECONDS=90
```

官方模式仅接受 `https://api.deepseek.com` 或其 `/v1` 根路径，不跟随重定向。密钥通过服务端 Authorization 发送，不进入浏览器、诊断材料或报告。

DeepSeek 当前默认开启思考模式。此项目显式关闭它，以便将有限输出预算用于诊断 JSON 与 patch；确需开启时可设为 `enabled`，并调整输出与任务预算。模型的 `reasoning_content` 不保存、不展示；仅保留最终结构化响应以及模型 ID、响应 ID、停止原因和用量。

## 验证连接

启动服务后，在创建任务页点击“检查模型连接”，或者运行：

```sh
uv run python scripts/check_model.py
```

该操作只请求 `/models`，验证鉴权及选中模型存在，不发起付费生成，也不能证明余额充足。`/health` 中的 `model_ready` 只表示配置齐全，不代表连接已经通过。

如需实际检查生成能力，显式运行：

```sh
uv run python scripts/check_model.py --generate
```

该命令先检查模型列表，再进行一次输出上限 512 tokens 的 JSON 生成，返回校验结果与服务商用量，不打印密钥或完整原始响应。通过这一步后，再从浏览器执行真实案例，验收失败复现、模型 patch 与最终独立测试。

## 错误与记录

| 代码 | 原因与处理 |
| --- | --- |
| `model_connection` | 后端网络或代理无法连接；不表示密钥无效 |
| `model_authentication` | HTTP 401，更新服务端密钥 |
| `model_insufficient_balance` | HTTP 402，为账户充值后重新诊断 |
| `model_not_found` | 检查 MODEL_ID、API 根地址及账户可用模型 |
| `model_http_error` | 限流或服务端错误，稍后重试；数字 Retry-After 会保留 |
| `model_output_truncated` | 输出达上限，提高输出预算或关闭思考模式 |
| `model_empty_response` | 空响应，可重新诊断 |
| `model_configuration` | 官方地址不合法，修正配置后重启服务 |

每次诊断调用都有独立记录，保存请求模型、服务商实际返回的模型名、起止时间、Token 预留与实际用量、结构校验结果和错误代码。未知用量继续按保守额度计入预算；未设置可靠计价来源时费用保持未知。固定策略的重试仍不属于 Harness² 自适应优化。

当前环境已完成鉴权、真实 JSON 生成及三个案例的浏览器修复验收，详见 [验证记录](verification.md)。日常自动化检查使用明确标注的模型测试替身；`npm run test:live` 才会触发真实模型页面验收并产生用量。模型也可交付逐字匹配的 `edits`，由平台构造标准 patch 后独立验证，避免模型手写 diff 行数错误。

官方参考：[首次 API 调用](https://api-docs.deepseek.com/)、[模型列表](https://api-docs.deepseek.com/api/list-models/)、[JSON 输出](https://api-docs.deepseek.com/guides/json_mode/)、[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。
