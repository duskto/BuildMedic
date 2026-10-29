# GitHub 读取重试与日志复用

GitHub 元数据和 Job 日志读取支持有限重试。缓存只减少同一会话重复诊断时的日志下载，不缓存仓库权限、Run 元数据或执行权限。

## 有限重试

连接、读取超时和 HTTP 500 / 502 / 503 / 504 最多尝试三次，通常等待 0.25、0.5 秒。每次重新读取完整响应，丢弃中断时的部分内容；元数据仍最多 4 MB，日志下载仍最多 2 MB。无效 JSON、超限、资源不存在、权限拒绝和不安全重定向直接失败。等待使用异步计时，Worker 的取消、租约和总耗时边界继续生效。

限流优先遵守 `Retry-After` 和额度耗尽时的 `x-ratelimit-reset`，同时存在时取更晚时间。仅在所需等待不超过 2 秒时自动重试。较长等待直接返回 `github_rate_limited`，通过 `error.details.retry_at` 给出 Unix 秒时间；预览页面显示浏览器本地时间并保留 Run URL。没有有效时间提示时，至少等待 60 秒后再手动重试。读取失败不会自动重建任务或追加模型预算。

这是对 GitHub [REST API 重试建议](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)的有界实现。GitHub 的[日志下载接口](https://docs.github.com/en/rest/actions/workflow-jobs#download-job-logs-for-a-workflow-run)返回的链接只有一分钟有效期。因此下载中断、临时服务错误或链接返回 401 / 403 时，会重新从 API 申请链接，再校验 HTTPS 和官方域名；不重复使用旧签名链接。API 的 Authorization 与 cookie 不会传给下载客户端，也不允许第二次重定向。

## 会话内缓存

Worker 先重新查询公开仓库、固定 Run attempt 和 Job，检查与创建时冻结的仓库、Run ID、attempt、SHA 及登记环境关联一致，再考虑缓存。未完成或状态未知的 Job 不缓存；空日志和失败响应不缓存。缓存命中也要求这轮在线身份核对成功，无法连网时不会跳过核对。

缓存键包含浏览器会话 owner、规范仓库名、Run ID、attempt、Job ID 和完整提交 SHA。不同会话或任何不同版本分别下载。只保存脱敏后的文本和绑定、内容 SHA256、字节数、创建 / 到期时间；不保存 Token、下载请求头、cookie 或签名 URL。查询参数出现在日志正文时也按既有规则脱敏。

读取时检查完整绑定、字节数、内容 hash、有效期以及当前脱敏规则。损坏、过期或新配置的凭据需要进一步脱敏时，删除旧项并重新下载该固定 Job。并发下载同一固定 Job 得到不同内容时返回 `github_log_changed`，保留先写入的有效项，不覆盖身份相同但内容不同的日志。

| 边界 | 上限 |
| --- | --- |
| 有效期 | 写入后 24 小时；命中不续期 |
| 每个会话 | 20 项 |
| 整个缓存 | 200 项、32,000,000 字节脱敏正文 |
| 单份缓存文本 | 6,000,000 字节，容纳下载后 UTF-8 替换字符展开 |

写入时先清理过期项，再从新到旧保留符合数量和字节限额的项。过期项立即停止提供复用，物理清理在后续写入时进行。缓存 TTL 不会删除已有任务的日志产物。网络请求位于数据库事务之外；SQLite 使用现有写事务序列化，PostgreSQL 使用事务级锁协调缓存发布与清理。

`BUILDMEDIC_GITHUB_LOG_CACHE_ENABLED=false` 可关闭复用和缓存写入。修改 API / Worker 配置后重启；已有缓存不会因此立即清空，后续启用并写入时继续按期限和容量清理。

## 来源归档与迁移

每个新任务独立保存自己的完整 `ci_log`，即使命中缓存也不跨任务链接产物。证据的可选 `log_origin` 标记 `source=network|cache`、固定绑定、SHA256、字节数和缓存时间，并指向 `ci_log_origin` JSON 来源产物。页面显示“本次从 GitHub 下载”或“复用本会话缓存”，可下载来源记录；旧任务缺少该字段时照常阅读。

新表通过 Alembic `0005` 创建。现有环境运行 `.venv/bin/python scripts/migrate.py` 后再启动 API / Worker；0004 升级保留任务和产物。缓存不提供浏览器直接查询接口，访问日志和来源记录仍沿用任务 owner 校验。

重试、临时下载链接、会话隔离、固定版本关联、损坏 / 过期处理、并发漂移、容量清理和页面显示均有明确的 HTTP / UI fixture 检查。真实 GitHub 失败 Run 的网页 / 模型 / Docker 闭环已验收，另核对在线重新查询后同会话缓存命中、无新增模型调用，见 [真实验收](github-acceptance.md)。
