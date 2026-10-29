# BuildMedic

CI 失败诊断与补丁验证工作台：读取失败日志，固定源码快照，由模型生成修复，再通过 Docker 独立测试验证结果。

**失败日志 → 问题复现 → 模型诊断与补丁 → 独立回归 → 证据报告**

## 主要功能

- **诊断与修复**：本地 pytest 案例、GitHub Actions 失败 Run、日志证据、补丁与报告下载。
- **模型接入**：DeepSeek、Kimi、GLM、OpenAI（ChatGPT 系列 API）及兼容服务，支持官方接口和中转站。
- **自适应诊断**：原生 Harness² 策略优化，以及可选的证据读取、源码检索、候选检查等工具调用。
- **独立验证**：固定 Docker 镜像、全新源码副本、目标测试与完整回归、取消与资源限制。
- **评测与经验**：四组对照、20 项公开构造任务、冻结指导、独立行为评分与离线人工审核。
- **任务管理**：实时进度、刷新恢复、幂等提交、历史记录；支持 SQLite 或 PostgreSQL / Redis / Celery。

## 快速开始

需要 **Python 3.12 / 3.13、Node.js 22+、uv 和已启动的 Docker**。以下命令在项目根目录执行。

### 1. 安装依赖并构建页面

```sh
uv sync --locked --extra adaptive
npm ci --prefix apps/web
npm run build --prefix apps/web
```

### 2. 配置环境变量

首次安装时复制模板；已有 `.env` 请保留并按需修改：

```sh
cp .env.example .env
```

在 `.env` 中填写：

| 配置                        | 用途                                                |
| --------------------------- | --------------------------------------------------- |
| `BUILDMEDIC_SESSION_SECRET` | 至少 32 字符的随机会话密钥                          |
| `BUILDMEDIC_RUNNER_TOKEN`   | 单独生成的内部 Runner 密钥                          |
| `BUILDMEDIC_MODEL_PROVIDER` | `deepseek`、`kimi`、`glm`、`openai` 或 `compatible` |
| `BUILDMEDIC_MODEL_BASE_URL` | 模型 API 根地址，程序会追加 `/chat/completions`     |
| `BUILDMEDIC_MODEL_API_KEY`  | 所用官方平台或中转站的密钥                          |
| `BUILDMEDIC_MODEL_ID`       | 对应账户可用的模型 ID                               |

选择具体服务商时，默认校验对应的官方地址。使用中转站时，另设 `BUILDMEDIC_MODEL_ENDPOINT_MODE=relay`，并填写中转站地址、密钥和模型 ID。

完整示例及协议差异见[模型接入说明](docs/models.md)。密钥仅保存在服务端，`.env` 已加入 Git 忽略规则。

### 3. 构建测试镜像

```sh
uv run --extra adaptive python scripts/build_runner.py
```

将输出的 `BUILDMEDIC_RUNNER_IMAGE=sha256:...` 写入 `.env`。

### 4. 启动项目

```sh
uv run --extra adaptive python scripts/dev.py
```

启动脚本会升级数据库，并启动 API、数据库 Worker 和 Runner。默认使用 SQLite，无需额外部署 PostgreSQL 或 Redis。

- 工作台：<http://127.0.0.1:8000>
- API 文档：<http://127.0.0.1:8000/docs>

从“新建诊断”选择本地案例即可运行。模型配置缺失时保留复现证据并提示阻塞。

需要自适应诊断时，在 `.env` 设置 `BUILDMEDIC_ADAPTIVE_ENABLED=true` 后重启。前端热更新使用 `uv run --extra adaptive python scripts/dev.py --web`，访问 <http://127.0.0.1:5173>。

## 检查与测试

不触发付费生成的模型列表检查：

```sh
uv run --extra adaptive python scripts/check_model.py
```

不支持模型列表接口时会明确显示“未验证”。加 `--generate` 会进行一次真实模型生成并产生用量。

日常回归使用模型测试替身：

```sh
uv run --extra adaptive pytest -q
npx --prefix apps/web playwright install --with-deps chromium
npm run test:e2e --prefix apps/web
```

显式 Docker 检查和真实模型浏览器验收需另行启用；`test:live` 会调用真实模型并产生费用。命令与结果见[验证记录](docs/verification.md)。

## 支持范围

当前面向登记的 **Python / pytest** 项目。GitHub 来源须登记仓库、提交和复现环境才能执行修复；未登记来源仅做日志诊断。项目不会自动创建远程修复 PR。

已有 DeepSeek、本地 Docker、一个登记 GitHub 案例及同机 PostgreSQL / Redis / Celery 故障恢复验收。新增模型接入经过协议替身测试，Kimi、GLM、OpenAI 和中转站仍需对应账户做真实验收。20 项构造任务尚无整批真实模型成绩或迁移收益结论。

当前适合本地与受控环境使用，公网、跨节点及负载运行尚未验收。详细范围见[部署验收](docs/deployment-acceptance.md)。

## 文档与目录

| 主题               | 文档                                                                                                                                           |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| 部署、数据库与队列 | [部署说明](docs/deployment.md) · [架构与信任边界](docs/architecture.md)                                                                        |
| 模型与远程仓库     | [模型接入](docs/models.md) · [GitHub 接入](docs/remote.md)                                                                                     |
| 自适应与工具       | [Harness²](docs/harness.md) · [Agent 工具](docs/agent-tools.md) · [日志证据](docs/logs.md)                                                     |
| 评测与经验         | [四组评测](docs/evaluation.md) · [分组任务](docs/grouped-evaluation.md) · [经验管理](docs/experience.md) · [冻结指导](docs/frozen-guidance.md) |
| 独立评分           | [行为评分](docs/scoring.md) · [人工诊断审核](docs/diagnosis-review.md)                                                                         |
| API 与验收         | [接口说明](docs/api.md) · [验证记录](docs/verification.md)                                                                                     |

```text
buildmedic/          API、任务调度、模型接入与 Runner
apps/web/           React 工作台及浏览器测试
scorer_service/     独立行为评分服务
harnesses/          自适应诊断的基础策略
packages/contracts/ OpenAPI 与 JSON Schema
scripts/            启动、迁移和验收工具
infra/              Docker 镜像与数据库基础服务
tests/              后端与集成测试
docs/               配置、设计和验收文档
```
