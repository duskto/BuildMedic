# BuildMedic

CI 失败诊断与补丁验证工作台。通过 React 页面创建任务，Python Worker 读取固定快照，在独立 Runner 复现失败、请求真实模型生成补丁，并用全新副本执行目标测试与完整回归。

当前版本 `0.2.0` 已完成 P0 本地标准诊断验收及一个登记公开案例的 P1 真实远程网页闭环：输入 GitHub 失败 Run，经真实 DeepSeek 与原生 Harness² 生成补丁，在 Docker 中独立验证通过；将实际选中补丁应用到验收分支后，GitHub 复跑也通过。范围限于登记的 Python / pytest 开发案例，见[验收记录](docs/github-acceptance.md)。

## 当前能力

- 工作台、创建任务、筛选分页列表、任务详情与移动端布局。
- 三个真实 pytest 缺陷案例：分页边界、内部导入、调用签名；另有环境阻塞和目标未复现两条失败路径。
- FastAPI、SQLite / PostgreSQL、持久事件与可恢复 SSE、产物下载、同源 HttpOnly 会话。
- 幂等创建、事务 outbox、数据库 Worker；可选 Celery / Redis 分发。
- 独立 Runner 的固定检查、进程组取消、JUnit 解析、环境与测试指纹。
- 补丁路径与范围校验、原始失败指纹检查、候选测试及最终独立验证；重新验证追加历史记录。
- Harness² k=1 顺序优化：真实策略提议、4 个独立 rollout、组合策略差异与最终独立验收。
- 真实模型的 Chat Completions 兼容接口；调用前预算预留、缺失用量标记、格式与证据引用校验。
- 可选原生 Agent 工具循环：按需读取证据、检索源码、提交候选、请求固定检查并交付诊断；标准和 Harness² 共享预算与独立验收，见[工具说明](docs/agent-tools.md)。
- 固定公开开发集的四组对照：日志解释、单次固定策略、独立多次固定策略与 Harness²；修复组共享相同预算上限，支持执行前登记、断点恢复、冻结证据、重复波动及配对消耗分析和离线复算，见[评测说明](docs/evaluation.md)。
- 可选独立私有行为评分：外部断言、断网候选容器、签名回执和误报 / 新增回归统计；不改写原公开报告，见[评分服务](docs/scoring.md)。
- 长日志失败定位：原始行号、步骤与时间戳索引，归档模型节选和省略范围，支持分页及超长行续读，见[日志证据](docs/logs.md)。
- GitHub 有限网络重试与会话内日志缓存：遵守限流时间、刷新下载链接、重新核对固定 Job 关联，归档日志来源，见[读取与复用](docs/github-ingestion.md)。
- Runner 故障分类与清理确认：区分 OOM、超时、异常退出和用户取消，保留最终状态后移除容器，见[故障处理](docs/runner-failures.md)。
- GitHub Actions Run 预览与固定 attempt 的 Job 读取。已登记的仓库 / SHA / workflow / Job 可进入固定 Docker 环境，进行标准或自适应修复；未登记来源只做日志诊断。登记表默认留空，见[远程仓库接入](docs/remote.md)。

## 本地开发

需要 Python 3.12 / 3.13、Node.js 22+、uv 和 npm。安装依赖：

```sh
uv sync --locked
cd apps/web
npm ci
cd ../..
```

Python 和前端依赖分别由 `uv.lock` 与 `apps/web/package-lock.json` 固定。

使用自适应诊断时，运行 `uv sync --locked --extra adaptive`，配置 `BUILDMEDIC_ADAPTIVE_ENABLED=true`，并用 `uv run --extra adaptive python scripts/dev.py --web` 启动。完整说明及固定上游 SHA 见 [Harness² 接入](docs/harness.md)。

配置并初始化数据库：

```sh
cp .env.example .env
uv run python scripts/migrate.py
```

如果此前使用自动建表的旧数据库，可运行 `uv run python scripts/migrate.py --adopt-existing`；程序只接管结构完全匹配的旧库，并先备份 SQLite。API/Worker 启动只检查迁移版本，开发启动脚本会先执行升级。

填写 `.env` 中的会话密钥、Runner 令牌与模型连接信息。模型配置变量是：

```dotenv
BUILDMEDIC_MODEL_BASE_URL=https://your-provider.example/v1
BUILDMEDIC_MODEL_API_KEY=your-server-side-key
BUILDMEDIC_MODEL_ID=your-explicit-model-id
```

DeepSeek 官方服务的配置和连接检查见 [DeepSeek 接入说明](docs/deepseek.md)。创建任务页可读取模型列表检查鉴权，也可运行 `uv run python scripts/check_model.py`；此检查不触发付费生成。

`MODEL_BASE_URL` 指兼容服务的 API 根地址，程序在其后追加 `/chat/completions`。服务须支持 JSON 对象输出与 `max_tokens` 参数。不自动选择模型；缺少配置时任务保留真实复现证据，结束为 `blocked`。费用未设置计价来源时始终显示未知。

默认使用 Docker Runner。构建预装 pytest 的镜像并记录实际 image ID：

```sh
uv run python scripts/build_runner.py
```

把输出的 `sha256:...` 填入 `BUILDMEDIC_RUNNER_IMAGE`，然后启动三个后端进程和 Vite：

```sh
uv run python scripts/dev.py --web
```

访问 `http://127.0.0.1:5173`，API 文档位于 `http://127.0.0.1:8000/docs`。Vite 代理 `/api`，浏览器无需接触 Runner 或模型密钥。

无 Docker 时，仅在你信任案例与模型生成代码的开发机器上，显式选择本机进程执行：

```sh
uv run python scripts/dev.py --trusted-local --web
```

该模式不提供容器隔离。它仍使用真实 pytest 进程和独立源码副本，不会用内置参考补丁冒充模型结果。

构建前端后可由 API 同域提供页面：

```sh
cd apps/web
npm run build
cd ../..
uv run python scripts/dev.py
```

## 队列与数据库

默认数据库为 `var/buildmedic.db`，默认 Worker 持续领取数据库 outbox 中的任务。API 不执行长任务。

若使用 PostgreSQL / Redis，可通过 `infra/compose.yaml` 启动基础服务，然后设置 `.env`：

```dotenv
BUILDMEDIC_DATABASE_URL=postgresql+psycopg://buildmedic:YOUR_PASSWORD@127.0.0.1:5432/buildmedic
BUILDMEDIC_REDIS_URL=redis://127.0.0.1:6379/0
BUILDMEDIC_QUEUE_BACKEND=celery
```

分别运行 API、Runner、outbox dispatcher 和 Celery：

```sh
uv run uvicorn buildmedic.api:app --host 127.0.0.1 --port 8000
uv run uvicorn buildmedic.runner:app --host 127.0.0.1 --port 8001
uv run python -m buildmedic.worker --dispatch
uv run celery -A buildmedic.worker:celery_app worker --concurrency=1 --loglevel=INFO
```

`compose.yaml` 只启动基础存储，Runner 应在独立执行节点运行。当前会话与配额适用于本地开发，尚未提供正式登录及公开服务的全局配额。完整部署边界见 [架构说明](docs/architecture.md)。

## 验证

```sh
uv run --extra adaptive pytest
uv run --extra adaptive python scripts/export_contracts.py
cd apps/web
npm run build
npx playwright install --with-deps chromium
npm run test:e2e
```

后端日常测试中的模型是明确标注的测试替身，目标检查、回归和独立验证启动真实 pytest 子进程。另有显式运行的 `npm run test:live`，通过浏览器连接真实服务与模型，会产生模型用量；执行方式和结果见 [验证记录](docs/verification.md)。

当前已验证：339 项日常后端检查、额外 20 项容器环境检查、31 项 fixture 浏览器检查，以及真实远程 Run 的网页 / 模型 / Docker 闭环和 TypeScript/Vite 生产构建。四组真实验收中日志组交付 3 份未评分诊断、未执行代码；三个修复组均 3/3 通过公开检查，单次固定策略消耗 9,980 Token，独立多次消耗 40,445 Token，Harness² 消耗 154,047 Token，尚未显示公开通过数收益。另用工作区外的私有行为包评分，9 个选中补丁均通过。真实远程验收成功一轮为 7 次调用 / 65,432 Token，连同首轮预算失败合计 13 次 / 118,395 Token；后续重新验证未新增模型调用。独立人工诊断审核流程与分组清单预检已接通，真实诊断准确率与分组隔离测试集评测仍待完成。经验库默认关闭；PostgreSQL/Redis 故障验收仍待完成。

## 工程入口

- `buildmedic/api.py`：HTTP API、会话与事件订阅。
- `buildmedic/orchestration.py`：标准诊断、预算、取消与独立验收。
- `scripts/check_model.py`：模型列表检查与显式的小额生成检查。
- `buildmedic/adaptive.py`：原生 Harness² 调度与受限 HTTP 执行桥。
- `buildmedic/agent_tools.py`：原生工具协议、任务内证据与候选检查循环。
- `buildmedic/evaluation.py`：预登记对照、证据冻结、公开指标与离线复算。
- `buildmedic/evaluation_analysis.py`：重复运行的描述统计、同案例配对与独立分析指纹。
- `buildmedic/logs.py`：失败窗口、模型片段选择及有界日志分页。
- `buildmedic/ci_logs.py`：固定 Job 的会话内脱敏日志缓存、完整性与容量管理。
- `buildmedic/scoring.py`、`scorer_service/`：独立评分桥、外部断言协调和有界容器观测。
- `buildmedic/diagnosis_scoring.py`：冻结诊断、离线人工审核与签名回执，见[操作指南](docs/diagnosis-review.md)。
- `buildmedic/evaluation_dataset.py`：仓库与缺陷模板分组、数据划分和指纹预检。
- `buildmedic/experiences.py`：同域经验版本、冻结输入和带证据的发布。
- `buildmedic/profiles.py`、`buildmedic/snapshots.py`：登记配置、受限归档与固定提交缓存。
- `buildmedic/worker.py`：outbox、Celery 与失效租约回收。
- `buildmedic/runner.py`：内部固定检查执行服务。
- `buildmedic/store.py`：快照、事件、产物、幂等与 fencing。
- `apps/web/src/`：React 工作台及 Zod 合同校验。
- `packages/contracts/`：后端导出的 OpenAPI / JSON Schema。
- `tests/`：后端和真实 Runner 集成。
- `apps/web/tests/`：日常浏览器场景与独立的真实模型验收。
- `scripts/migrate.py`：数据库升级与严格的旧库接管。
- `scripts/register_profile.py`：下载固定提交并生成待核对的登记文件。
- `scripts/run_evaluation.py`、`scripts/recompute_evaluation.py`：恢复运行与断网复算开发对照。
- `scripts/analyze_evaluation.py`：从冻结证据断网复算波动与配对统计，不请求模型。
- `scripts/run_scoring.py`：对已有冻结实验启动 / 恢复独立评分，不请求模型。

[Harness² 接入](docs/harness.md) · [Agent 工具诊断](docs/agent-tools.md) · [日志证据](docs/logs.md) · [开发集评测](docs/evaluation.md) · [独立评分](docs/scoring.md) · [远程仓库接入](docs/remote.md) · [任务经验管理](docs/experience.md) · [接口说明](docs/api.md) · [开发验证记录](docs/verification.md)

开发指南、凭据、缓存、依赖目录和运行数据由 `.gitignore` 排除。此工作区已有本地 Git 元数据保存在 `.git-local`，在此工作区使用 `git --git-dir=.git-local ...`。
