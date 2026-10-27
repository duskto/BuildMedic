# BuildMedic

CI 失败诊断与独立补丁验证平台，当前提交保存开发中的工程基础。

## 已有内容

- Python 项目配置及 `uv.lock` 依赖锁文件。
- 任务、事件、产物、执行租约和 outbox 的 SQLAlchemy 数据模型。
- 会话签名、日志脱敏、任务幂等创建和状态持久化逻辑。
- 只允许修改 `src/` 下既有 Python 文件的 unified diff 校验与应用。
- 独立 Runner 服务，提供固定 pytest 检查、JUnit 解析和执行取消。
- 分页边界、内部导入、调用签名三个真实失败测试案例。
- React / TypeScript 前端依赖声明。

## 开发状态

项目尚未形成可用的端到端产品：Web 页面、业务 API、Worker 编排、模型调用、GitHub Actions 接入和 Harness² 适配仍需实现。Runner 及现有模块也尚未完成集成验证。

案例中的缺陷是故意保留的，原始测试预期失败。不要将它们的失败误认为平台自身的测试结果。

## 环境配置

使用 Python 3.12 或更新版本，通过 `uv sync --locked` 安装依赖。复制 `.env.example` 为 `.env` 后填写本地配置。默认数据库为 SQLite，PostgreSQL 可通过环境变量配置。

Runner 需要独立令牌和固定 `sha256:` 镜像。默认采用 Docker 后端；`trusted-local` 仅用于明确受信任案例的本地开发，它不提供容器隔离。当前仓库尚未提供镜像构建和完整启动脚本。

开发指南、凭据、本地缓存、依赖目录和运行数据由 `.gitignore` 排除；依赖锁文件及 `.env.example` 保留在版本控制中。
