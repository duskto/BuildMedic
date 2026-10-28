# API 合同

API 根路径为 `/api/v1`。成功返回 `{data, request_id}`，错误返回 `{error:{code,message,retryable,details}, request_id}`。会话使用 HttpOnly / SameSite=Strict cookie；所有任务和产物都检查会话归属。运行时 OpenAPI 位于 `/openapi.json`。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 模型、Runner 与自适应模式就绪状态 |
| POST | `/model/check` | 服务端鉴权及模型列表检查，不触发生成 |
| GET | `/demo-cases` | 五条案例和固定源码 SHA256 |
| GET | `/stats` | 当前会话的真实统计 |
| POST | `/ci/preview` | 固定 Run attempt，分页读取失败 Job |
| POST | `/tasks` | 幂等创建；必须传 Idempotency-Key |
| GET | `/tasks` | lifecycle / repo 筛选及分页 |
| GET | `/tasks/{id}` | 一致快照、版本与最后事件序号 |
| GET | `/tasks/{id}/events` | SSE、Last-Event-ID / after 补发 |
| POST | `/tasks/{id}/cancel` | 幂等取消任务 |
| POST | `/tasks/{id}/retry` | 创建 parent_task_id 关联的新任务 |
| GET | `/tasks/{id}/candidates` | 真实候选列表 |
| GET / POST | `/tasks/{id}/validations` | 查看历史或排队重新独立验证 |
| GET | `/tasks/{id}/artifacts/{aid}` | 受授权的产物；download=true 下载 |
| POST | `/tasks/{id}/experience` | 幂等发布通过最终独立验证的经验草稿；需要 Idempotency-Key |
| GET | `/experience` | 当前会话经验列表；domain 筛选与分页 |
| GET | `/experience/{id}` | 完整经验、采纳策略与来源证据 |
| POST | `/experience/{id}/revoke` | 撤销对后续任务的可用性，保留历史 |
| GET | `/experience/snapshots/{id}` | 当前会话可见的冻结任务输入 |

创建本地任务：

```json
{"source":{"type":"demo","case_id":"pagination"},"mode":"standard","limits":{"max_wall_seconds":600,"max_model_calls":8,"max_tokens":48000}}
```

GitHub source 必须同时包含 `run_url`、`run_attempt`、`job_id`。服务端重新查询并确认 Job 属于固定 attempt 和 SHA。仅公开仓库；当前版本执行能力为日志诊断，无法自动复现远程仓库。需要读取受保护日志时在服务端配置 GitHub Token。

GitHub URL 仅接受 `https://github.com/owner/repo/actions/runs/id`，可使用 `?attempt=2` 固定运行次数。下载仅跟随经过主机校验的官方日志重定向，并使用独立 HTTP client，避免把 API Authorization 透传给下载主机。

`lifecycle`、`phase`、`outcome` 和验证状态分别表达平台执行、业务阶段、结论及独立验收。`completed + unresolved` 是正常业务结束；`completed + repaired` 必须有原失败复现与最终独立检查证据。

重新验证创建新的 validation 记录，不改变第一次验收的历史结论。页面通过轮询接收已结束任务的后续验证变化。最多允许同一任务有一条进行中的重新验证。

内部 Runner API 位于 `/runner/v1`，必须携带独立 Bearer token，不能直接暴露给浏览器。支持 health、提交 execution、读取 execution、取消 execution；结果包含命令、退出原因、日志、JUnit 计数、patch hash、环境与测试指纹。

任务快照的 `model_calls` 保存逐次调用状态、请求与响应模型、用量与耗时；它只记录可审核的执行事实，不保存 `reasoning_content`。`phase_history` 记录实际到达过的阶段，提前结束后不把未执行阶段显示为完成。

自适应模式 `mode=adaptive` 仅接受 demo 来源，需安装固定版本 Harness²、开启服务端开关并配置模型。创建时至少 7 次调用 / 48,000 Token 预算，推荐 12 次 / 100,000。未知能力、远程来源和不足预算分别返回 `adaptive_unavailable`、`adaptive_source_unsupported`、`adaptive_budget_too_small`。

快照的 `harness2_sha` 与 `harness` 记录原生调度版本、k/mode、提议差异、阶段回退和组合采用结果；`model_calls.role` 区分诊断与策略提议，包含全部格式修正请求。SSE 增加 `harness.started`、`harness.proposed`、`harness.finished` 与 `harness.interrupted`，可按已有游标恢复。

`experience=true` 仅允许 adaptive demo，至少 8 次模型调用，推荐新任务使用 12 次 / 150,000 Token；此字段缺省为 false。只有主动开启反思时才能申请超过 100,000 的预算，最高 150,000。已有任务及标准诊断的上限保持不变。

任务快照的 `experience` 记录创建时冻结的输入版本/指纹、反思状态、验证后的草稿和已发布版本 ID。`experience_reflection` 模型调用与其他角色共用预算。SSE 增加 `experience.reflecting`、`experience.reflected`、`experience.rejected`、`experience.available`、`experience.unverified`、`experience.published`。发布只使用服务器生成的草稿，不接收客户端编造的经验或策略，取消、未修复或证据不匹配返回 409。重复发布同一来源任务返回原版本；不同来源使用已占用的幂等键返回 409。全部接口检查当前会话归属，越权返回 404。详见[经验管理](experience.md)。
