# 同域任务经验

任务经验库基于固定版本 Harness² 的原生 `Library` 和 `reflect_adoption`，按当前浏览器会话隔离，默认关闭。所有条目标记为 `development_only`。来源补丁通过独立验证，只说明该补丁在登记环境中成立，不能据此宣称经验提高了其他任务的成功率。

## 使用

1. 创建演示任务，选择“自适应诊断”，勾选“使用同域经验并整理任务反思”。
2. 提交时绑定当前会话、同领域、策略 profile 兼容的最新未撤销版本，最多一条。没有匹配项时绑定空库。
3. 原生调度完成组合采纳后，使用同一 HTTP 模型整理反思。正常流程为 4 次诊断、3 次提议和 1 次反思，至少 8 次调用；格式修正请求也计入预算。开启后页面推荐新任务使用 12 次调用 / 150,000 tokens，可手动降低；原有任务预算不会改变。
4. 最终独立验证通过后，在任务“策略记录”查看经验草稿及证据，点击“发布到我的经验库”。发布操作不再调用模型。
5. 从侧栏“任务经验库”查看领域、版本、来源任务、证据 ID、采纳策略、模型和指纹。撤销只影响后续输入选择，历史条目仍可查看。

预算不保证足够完成每次运行。只有主动开启反思的新任务可选择超过原先 100,000 的上限，最高 150,000；请求、响应、保守预留与格式修正均计入同一个账本。硬限制触发后不会自动追加额度。

清除会话 cookie 或更换会话签名密钥后，不能再通过原浏览器访问旧会话数据。此处没有账号间共享或公共经验市场。

## 冻结与发布

创建任务和绑定经验在同一个数据库事务中完成。`experience_snapshots` 保存完整条目内容与 SHA256；任务快照包含其 ID、指纹、库版本与条目摘要。后续发布、撤销不会改变已有任务的输入。重试复制原输入内容与指纹，创建新的快照 ID；策略 profile 不兼容时停止执行，不偷偷换用新库。

Worker 将冻结内容还原到 `<data_dir>/harness/<task>/<lease>/library`。原生 Library 在 INV / DOM / COMPOSE 上下文中提供同域经验和完整采纳策略；基础 rollout 仍使用基础策略。上游反思及自动 `Library.add` 仅写入这个任务私有目录，不能直接发布到共享数据库。原生框架完成采纳时，最终独立验证尚未发生。

反思采用严格 JSON schema，校验 cue、lesson、单行摘要和当前任务的真实证据 ID。允许返回无条目，不将每个成功任务强行转成经验。校验失败可进行一次受预算约束的格式修正；取消、预算和 lease 硬停止仍贯穿原生容错调用。

只有最终独立验证通过，Worker 才归档 `experience_draft`。发布事务重新核对：

- 会话归属与任务 `completed + repaired`。
- 反思完成的模型调用及其策略指纹。
- 所有证据 ID 来自该任务。
- 草稿对应选中候选、rollout、完整策略归档和补丁指纹。
- 指定验证是该候选通过的最终独立验证，目标与回归使用相同补丁。
- 输入快照、策略 profile 与归档内容指纹一致。

发布写入不可变 `experience_versions.content`。每个来源任务最多一个版本；重复请求返回原条目。当前会话的发布、撤销与任务创建共用事务锁：SQLite 使用 `BEGIN IMMEDIATE`，PostgreSQL 使用 owner advisory transaction lock。领域内版本号单调递增，不覆盖旧内容。并发任务各自保留实际输入的 `parent_version_ids`，不会把刚发布的其他版本假装成自己的父版本。

`guardrails.md`、`LongTermMEMORY.md` 继续只读。经验不会改变执行命令、工具权限、依赖、测试、源码修改范围或预算。HTTP 上下文对逐字相同的文件内容去重，`context_file_aliases` 保留所有原路径；没有删除文件或截掉证据。完整上下文过大仍正常触发预算保护。

## 接口与迁移

`CreateTask.experience` 为严格布尔值，只允许 adaptive demo。旧请求缺省为 false，幂等请求指纹保持兼容。

| 接口 | 行为 |
| --- | --- |
| `GET /api/v1/experience` | 当前会话的版本列表，支持 domain、page、page_size |
| `GET /api/v1/experience/{id}` | 完整内容与来源记录 |
| `POST /api/v1/tasks/{id}/experience` | 发布服务器已有草稿，必须提供 Idempotency-Key |
| `POST /api/v1/experience/{id}/revoke` | 幂等撤销，不修改历史内容或已绑定快照 |
| `GET /api/v1/experience/snapshots/{id}` | 当前会话可见的冻结输入 |

Alembic `0002` 新建两个表；已有 `0001` 数据库可原地升级，不重建或清空任务。`scripts/dev.py` 启动前自动升级，单独运行服务前执行 `uv run --extra adaptive python scripts/migrate.py`。

已有[公开开发集同预算对照](evaluation.md)，其中经验库关闭。held-out 数据分割、冻结经验的效果对照和经验收益指标尚未实现；没有把本地案例重复运行视为泛化评测。PostgreSQL 的发布并发与 Redis 故障恢复还需专门的运行环境验收。
