# 部署说明

默认本地启动使用 SQLite、数据库 Worker 和 Docker Runner，步骤见 [README](../README.md)。本文补充独立进程、数据库及队列配置；同机部署的验收结果见[部署验收记录](deployment-acceptance.md)。

## 数据库迁移

`scripts/dev.py` 会先升级数据库。分进程启动时，先单独执行：

```sh
uv run --extra adaptive python scripts/migrate.py
```

接管曾使用自动建表的旧库时，使用 `scripts/migrate.py --adopt-existing`。程序只接管结构匹配的数据库，并先备份 SQLite。API 和 Worker 启动时检查迁移版本，不会自行建表。

## PostgreSQL / Redis / Celery

先通过环境变量设置 `POSTGRES_PASSWORD`，再启动基础存储：

```sh
docker compose -f infra/compose.yaml up -d --wait
```

Compose 仅启动 PostgreSQL 和 Redis，数据保存在具名卷中。服务默认只绑定本机 5432 / 6379 端口。

修改项目 `.env`；数据库密码须与 Compose 使用的密码一致，URL 中的特殊字符须编码：

```dotenv
BUILDMEDIC_DATABASE_URL=postgresql+psycopg://buildmedic:YOUR_URL_ENCODED_PASSWORD@127.0.0.1:5432/buildmedic
BUILDMEDIC_REDIS_URL=redis://127.0.0.1:6379/0
BUILDMEDIC_QUEUE_BACKEND=celery
```

完成迁移后，在四个终端分别启动 API、Runner、outbox dispatcher 和 Celery 消费进程：

```sh
uv run --extra adaptive uvicorn buildmedic.api:app --host 127.0.0.1 --port 8000
uv run --extra adaptive uvicorn buildmedic.runner:app --host 127.0.0.1 --port 8001
uv run --extra adaptive python -m buildmedic.worker --dispatch
uv run --extra adaptive celery -A buildmedic.worker:celery_app worker --concurrency=1 --loglevel=INFO
```

前端先运行 `npm run build --prefix apps/web`，然后访问 `http://127.0.0.1:8000`。四个进程须使用一致的数据库、模型配置及 Runner 连接信息。API 和 Worker 共用会话配置；Runner 内部令牌必须匹配。

`scripts/dev.py` 在 Celery 模式下只启动分发进程，不启动 Celery 消费进程，后者仍须单独运行。

## 前端开发与可信本机执行

- 前端热更新：`uv run --extra adaptive python scripts/dev.py --web`，访问 `http://127.0.0.1:5173`；Vite 将 `/api` 转发到 API。
- 无 Docker 时，可显式运行 `uv run --extra adaptive python scripts/dev.py --trusted-local --web`。此模式在宿主机执行案例和模型补丁，不提供容器隔离，仅用于完全可信的开发环境。

## 部署边界

Runner 拥有 Docker 执行权限，应保留在受信任的独立执行边界，不向浏览器开放。独立评分服务有单独的外部私有目录和内部凭据，部署步骤见[评分服务说明](scoring.md)。

当前会话与配额面向本地开发，尚无正式登录和面向公开服务的全局配额。公网 HTTPS、代理、跨节点权限及负载运行尚未验收，详见[架构说明](architecture.md)与[验收范围](deployment-acceptance.md)。
