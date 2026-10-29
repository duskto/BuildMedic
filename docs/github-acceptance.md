# 真实 GitHub Run 验收

2026-10-29 已完成一个登记公开 Python / pytest 案例的 P1 核心验收：网页输入真实失败 Run URL，经本地 GitHub 接入、真实 DeepSeek 与原生 Harness² 生成补丁，在固定 Docker 中独立验证通过。随后由验收操作将实际选中补丁应用到独立分支，GitHub 复跑也通过。范围限于这个明确带缺陷的公开开发案例，不代表任意仓库支持或策略效果提升。

## 已取得的真实运行

| 绑定 | 值 |
| --- | --- |
| 仓库 | `duskto/BuildMedic` |
| 验收分支 | `acceptance/github-pagination-20261029` |
| Run | [40837098943](https://github.com/duskto/BuildMedic/actions/runs/40837098943) |
| attempt | `1` |
| 固定 SHA | `ae674195202ce5192ea9a38d08382a1ecad96c5e` |
| Job | `pytest` / `123324334613` |
| workflow | `.github/workflows/acceptance.yml` |
| 失败步骤 | `Reproduce the intentional pagination defect` |
| GitHub 实际结果 | 2 failed / 6 passed，Job 与 Run 均为 failure |
| 本地真实归档复现 | 2 failed / 6 passed，Docker 原问题复现，清理确认通过 |
| 源码清单 SHA256 | `a3abb17238dbc7516339b9bc01583255a18ae0a273c5d986ff41b1330b7c5d5f` |
| 网页任务 | `31c3aff1d1f14c15b1008e7e8d219658` |
| 修复后 Run | [40842086320](https://github.com/duskto/BuildMedic/actions/runs/40842086320) |
| 修复后 SHA | `8e15265f10c352128ac97f15c288ffc6bcc10db5` |
| 修复后 Job | `123340615914`，8 passed，Run / Job 均为 success |

这次 Run 是专门为验收建立的已知分页缺陷，源文件保存在 `fixtures/github_acceptance/pagination`。它是已有公开开发案例，不能作为新的独立缺陷样本或隔离测试集。独立分支只有该案例的 5 个文件；发布没有包含本地应用的未提交改动。初始读取使用 GitHub 连接器；完整网页验收使用本地服务的真实 GitHub API 和日志下载客户端，未替换请求或回放响应。源码实际下载自 codeload 固定 SHA，没有用本地目录替代远端源码。

Python 固定为 3.12.16；`requirements.lock` 中 pytest 与四个传递依赖均与本地固定镜像一致。实际测试镜像是 `sha256:23449681af91bd411cb891effde770f0b081af16472495ef3423b9800381535e`。在发布前还检查过已知参考补丁可使本地 8 项检查通过；那是确定性预检，未调用模型、未发布参考补丁，也未把它当成 Agent 成果。

本地证据在忽略目录：

- `var/acceptance/github-real-ci.log`：通过连接器读取并脱敏的真实 Job 日志。
- `var/acceptance/github-real-reproduction.json`：实际 GitHub 绑定、完整 profile、归档指纹、Docker 结果及清理事实。
- `var/acceptance/github-runner-profiles.json`：该固定提交的登记文件。
- `var/acceptance/github-publication-binding.json`：独立分支与发布文件清单。
- `var/acceptance/github-live-task.json`：成功任务及后续重新验证结果。
- `var/acceptance/github-live-budget-exhausted.json`：首轮预算耗尽的原始结果。
- `var/acceptance/github-remote-success.json`：实际选中补丁与远端成功 Run 的绑定。
- `var/acceptance/github-cache-smoke.json`：在线重新核对 GitHub 后的同会话缓存命中证据。

## 真实模型与独立验证

成功任务使用原生 `run_task(k=1, modes=("seq",), seed=42)`，4 个 rollout 和 INV / DOM / COMPOSE 三阶段均完成，三个提议均有实际策略修改且没有回退。初验共 12 次独立 Docker 检查；选中 c1 的补丁通过额外干净副本的目标与回归检查，均为 8 passed，测试身份与数量未缩减。

| 运行 | 调用数 | 服务商报告 Token | 结果 |
| --- | ---: | ---: | --- |
| 首轮，100,000 预算 | 6 | 52,963 | 两次提议格式拒绝，组合阶段前触发保守预留，budget_exhausted |
| 新任务，显式 150,000 预算 | 7 | 65,432 | repaired，最终目标 / 回归均 8/8 |
| 两轮合计 | 13 | 118,395 | 包含失败调用；费用未知，不估造 USD |

首轮没有改写为成功或追加上限。新任务从页面选择并固化 150,000 额度；非工具的登记远程自适应任务现在允许该上限，标准模式和普通本地自适应仍为 100,000。已有任务和关联重试继续保留原额度。保守 UTF-8 预留、实际用量结算和所有共享预算规则均保留。

成功后的真实页面重新验证另执行两次全新 Docker 检查，仍为 8/8，通过后任务调用数与 Token 保持 7 / 65,432。刷新恢复、报告下载、patch 内容指纹、跨会话任务 / 产物 404、窄屏布局和脚本错误检查通过。截图为 `github-live-strategy.png`、`github-live-mobile.png`。

验收操作只应用选中的模型 patch，改动仅为 `src/demo/pagination.py` 的切片终点一行，patch SHA256 为 `16b92e9deb3fd686629dca62c8ea3f899aefe8021de3175289962562e47efe45`；测试、workflow、依赖均未改变。GitHub 后续实际 Run 已核对为 8 passed。这是人工验收步骤，不表示产品已提供自动推送、PR 或远端结果回填功能；原任务仍固定失败 Run 的旧 SHA，不改写为新提交。

## 继续完整网页验收

本次已使用本机 `.env` 中的 `BUILDMEDIC_GITHUB_TOKEN` 完成真实读取；凭据未输出或写入浏览器。公开资源也可以在匿名额度可用时读取；连接器的认证不会自动转交给本地服务。若使用 fine-grained Token，仅选择该仓库，赋予 Actions: Read 与 Contents: Read 即可；读取 Job 日志需要的权限见 [GitHub 官方说明](https://docs.github.com/en/rest/actions/workflow-jobs#download-job-logs-for-a-workflow-run)。

如需重建登记文件：

```sh
.venv/bin/python scripts/register_profile.py \
  --id github-pagination-20261029 \
  --repository duskto/BuildMedic \
  --sha ae674195202ce5192ea9a38d08382a1ecad96c5e \
  --workflow .github/workflows/acceptance.yml \
  --job pytest --target tests/test_pagination.py --fingerprint test_pages \
  --require-file requirements.lock \
  --output var/acceptance/github-runner-profiles.json
```

已有该 ID 时无需重复登记。启动 API、Worker 和 Runner 时使用同一份文件；以下命令使用项目现有本地端口与配置：

```sh
BUILDMEDIC_RUNNER_PROFILES=./var/acceptance/github-runner-profiles.json \
BUILDMEDIC_ADAPTIVE_ENABLED=true \
  .venv/bin/python scripts/dev.py
```

另一个终端执行显式付费验收。前置条件为 Chromium 已安装且依赖可用；本工作区现有 QA 共享库配置位于忽略目录 `var/qa/browser-deps`。

```sh
cd apps/web
BUILDMEDIC_GITHUB_ACCEPTANCE_RUN_URL='https://github.com/duskto/BuildMedic/actions/runs/40837098943?attempt=1' \
BUILDMEDIC_GITHUB_ACCEPTANCE_SHA=ae674195202ce5192ea9a38d08382a1ecad96c5e \
BUILDMEDIC_GITHUB_ACCEPTANCE_JOB_ID=123324334613 \
  npm run test:live -- --grep 'github:'
```

脚本从真实页面预览和选择登记 Job，调用原生 Harness²，核对四个 rollout、策略改动、原失败复现、独立目标 / 回归和补丁指纹、CI 日志绑定、刷新恢复及跨会话 404。缺少明确 Run / SHA / Job 或 live 开关时跳过，避免日常测试误发付费请求。

任务和会话分别保存在 `github-live-task.json`、权限 0600 的 `github-live-session.json`；均位于忽略目录。页面检查中断后可沿用相同三个绑定变量并加 `BUILDMEDIC_GITHUB_ACCEPTANCE_RESUME=1`，读取原任务，不再新建付费诊断。失败任务保持失败，脚本不会偷偷重试模型修复。

live 脚本已通过真实运行和同任务恢复检查，恢复时不新增模型调用，也不重复创建已存在的重新验证。正式登录与部署、其他仓库 / 环境、带工具远程模式和分组隔离评测仍需单独验收。
