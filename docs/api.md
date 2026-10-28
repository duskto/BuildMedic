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
| POST / GET | `/experiments` | 幂等登记固定开发集清单 / 当前会话的对照列表 |
| GET | `/evaluation/cases` | 可登记案例的源码与公开日志指纹 |
| GET | `/experiments/{id}` | 同预算执行与冻结结果的统计 |
| POST | `/experiments/{id}/advance` | 返回当前任务或原子安排下一项；可安全重复 |
| POST | `/experiments/{id}/cancel` | 停止后续安排并取消当前任务 |
| GET | `/experiments/{id}/report` | 下载含冻结输入的离线复算证据包 |
| GET | `/experiments/{id}/analysis` | 重复轮次与同案例配对的描述统计，绑定来源报告 hash |
| GET | `/experiments/{id}/analysis/report` | 下载独立分析及来源证据包；不修改公开报告与评分回执 |
| POST / GET | `/experiments/{id}/score` | 启动 / 查询整批完成后的独立评分，不接收请求体 |
| GET | `/experiments/{id}/score/report` | 下载经服务端验证的评分回执，检查会话归属 |

创建本地任务：

```json
{"source":{"type":"demo","case_id":"pagination"},"mode":"standard","limits":{"max_wall_seconds":600,"max_model_calls":8,"max_tokens":48000}}
```

GitHub source 必须同时包含 `run_url`、`run_attempt`、`job_id`。服务端重新查询并确认 Job 属于固定 attempt 和 SHA。仅公开仓库；匹配管理员登记的仓库 / SHA / workflow / Job 后支持本地独立复现与修复，未登记来源仅做日志诊断。需要读取受保护日志时在服务端配置 GitHub Token。浏览器不能提交 profile、命令或镜像。

GitHub URL 仅接受 `https://github.com/owner/repo/actions/runs/id`，可使用 `?attempt=2` 固定运行次数。下载仅跟随经过主机校验的官方日志重定向，并使用独立 HTTP client，避免把 API Authorization 透传给下载主机。

`lifecycle`、`phase`、`outcome` 和验证状态分别表达平台执行、业务阶段、结论及独立验收。`completed + unresolved` 是正常业务结束；`completed + repaired` 必须有原失败复现与最终独立检查证据。

重新验证创建新的 validation 记录，不改变第一次验收的历史结论。页面通过轮询接收已结束任务的后续验证变化。最多允许同一任务有一条进行中的重新验证。

内部 Runner API 位于 `/runner/v1`，必须携带独立 Bearer token，不能直接暴露给浏览器。支持 health、提交 execution、读取 execution、取消 execution；结果包含命令、退出原因、日志、JUnit 计数、patch hash、环境与测试指纹。

任务快照的 `model_calls` 保存逐次调用状态、请求与响应模型、用量与耗时；它只记录可审核的执行事实，不保存 `reasoning_content`。`phase_history` 记录实际到达过的阶段，提前结束后不把未执行阶段显示为完成。

自适应模式 `mode=adaptive` 接受 demo 和已登记的 GitHub Job，需安装固定版本 Harness²、开启服务端开关并配置模型。创建时至少 7 次调用 / 48,000 Token 预算，推荐 12 次 / 100,000。未知能力、未登记的远程来源和不足预算分别返回 `adaptive_unavailable`、`adaptive_source_unsupported`、`adaptive_budget_too_small`。

预览返回每个 Job 的 `execution_supported` 与 `profile_version`；页面据此开启远程自适应入口。任务创建时保存服务器生成的 `runner_profile` 绑定，准备后增加 `source_snapshot` 和 `snapshot.prepared` 事件。该快照含完整性清单与完整 profile 的产物 ID，继续通过任务归属检查后下载。

内部 `POST /runner/v1/snapshots` 接受 `RemoteSnapshot` 绑定，仅准备已登记的公开固定提交；`ExecutionRequest` 必须在 `case_id` 与 `remote` 绑定中二选一。远程执行要求已验证缓存和完全匹配的 Docker 镜像，拒绝可信本机模式。两个接口均使用内部 Runner Bearer 令牌；浏览器没有调用入口。详见[远程接入](remote.md)。

快照的 `harness2_sha` 与 `harness` 记录原生调度版本、k/mode、提议差异、阶段回退和组合采用结果；`model_calls.role` 区分诊断与策略提议，包含全部格式修正请求。SSE 增加 `harness.started`、`harness.proposed`、`harness.finished` 与 `harness.interrupted`，可按已有游标恢复。

`experience=true` 仅允许 adaptive demo，至少 8 次模型调用，推荐新任务使用 12 次 / 150,000 Token；此字段缺省为 false。未开启工具时，只有主动开启反思才能申请超过 100,000 的预算，最高 150,000。

`agent_tools=true` 显式启用原生工具循环，支持标准与自适应的登记来源，缺省关闭。工具任务最高 32 次调用 / 250,000 Token，且不能超过 `/health.max_model_calls`。标准至少 4 次 / 24,000 Token，自适应至少 11 次 / 48,000 Token，含经验反思时至少 12 次。前置配置不满足返回 `agent_tools_unavailable`，预算不足返回 `agent_budget_too_small`。未开启工具的任务仍最多 12 次调用，旧请求幂等 hash 兼容。`/health.agent_tools_ready` 表示配置就绪；不探测付费工具生成能力。快照增加 `agent_tools`、`agent_actions`，事件增加 `agent.tool_started` / `agent.tool_finished`，详见[工具边界与预算](agent-tools.md)。

开发集对照支持 `schema_version=2.0` 的四组协议，要求固定本地案例、源码及公开日志 hash、修复组相同预算、重复次数与工具开关，经验库关闭。`multi_attempts` 为 2–4，默认 4；日志组最多 2 次调用且不使用源码或 Runner。旧版 `1.0` 保留原两组行为及复算口径。创建需要 Idempotency-Key，不执行模型；推进使用现有 Worker 与任务配额，不接受任意任务 ID 或客户端指标。四组任务的关联重试返回 `experiment_retry_unsupported`，重复实验需新建清单。快照的 `experiment_binding` 记录归属和组别，日志组 `reproduction_status=not_run`；诊断准确率未评分时保持 null。详见[评测 API](evaluation.md)。

独立评分只处理完成并冻结的四组 Docker 实验，每份实验保留一个固定请求；重复 POST 返回原请求，GET 可在服务恢复后推进同一请求。未启动时 GET 返回 null，未配置时 POST 返回 `scoring_unavailable`。签名、候选或请求绑定不匹配会拒绝回执；服务暂时不可达时保留 pending / running，不填造分数。完成的回执可在评分服务离线后继续读取。`/health.scoring_ready` 仅表示配置就绪。新评分表与旧任务 / 公开报告分开，原报告 hash 及 `hidden_verified=null` 不变，详见[指标口径](scoring.md)。内部 `/scorer/v1/jobs` 与 `/scorer/v1/jobs/{id}` 只用于受认证的 API / 评分节点通信。

任务快照的 `experience` 记录创建时冻结的输入版本/指纹、反思状态、验证后的草稿和已发布版本 ID。`experience_reflection` 模型调用与其他角色共用预算。SSE 增加 `experience.reflecting`、`experience.reflected`、`experience.rejected`、`experience.available`、`experience.unverified`、`experience.published`。发布只使用服务器生成的草稿，不接收客户端编造的经验或策略，取消、未修复或证据不匹配返回 409。重复发布同一来源任务返回原版本；不同来源使用已占用的幂等键返回 409。全部接口检查当前会话归属，越权返回 404。详见[经验管理](experience.md)。
