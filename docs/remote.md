# 登记远程仓库与固定 SHA 复现

已接通公开 GitHub Actions Run 的固定提交下载、登记环境执行、标准诊断、Harness² 自适应诊断和补丁独立验收。默认登记表为空；未登记的 Run 仍只做日志诊断。远程任务的经验库暂未开放。

## 登记与启动

当前支持规模受限的 Python 仓库：工作区内使用 `src/` 源码布局和 `tests/` 测试目录，目标为一个 `tests/**/*.py` 文件，完整回归固定为 `tests`。依赖必须事先装进固定 SHA256 的 Docker 镜像，运行时不会安装依赖，也不会执行 workflow 中的 shell 命令。

先从失败 Run 预览中取得真实仓库、完整 `head_sha`、workflow 路径和 Job 名称。把 `BM_REMOTE_REPOSITORY` 与 `BM_REMOTE_SHA` 设置为这次运行的真实值，再按仓库实际情况调整下列 workflow、Job、测试文件和失败指纹：

```sh
uv run --extra adaptive python scripts/register_profile.py \
  --id registered-pytest-v1 \
  --repository "$BM_REMOTE_REPOSITORY" \
  --sha "$BM_REMOTE_SHA" \
  --workflow .github/workflows/tests.yml \
  --job pytest \
  --target tests/test_pagination.py \
  --fingerprint test_pages \
  --output var/runner-profiles.json
```

脚本只下载和检查公开归档，不运行仓库代码。它记录完整源码清单、workflow 和可选依赖文件的 SHA256，生成待核对的登记文件。省略 `--output` 可先查看 JSON；指定输出时，在文件锁内检查重复 ID / Job 并原子追加，不覆盖既有登记。

镜像默认使用 `.env` 的 `BUILDMEDIC_RUNNER_IMAGE`，也可显式传 `--image sha256:...`。包位于子目录时传 `--workspace path/to/package`；workflow 路径始终相对仓库根。使用 `--require-file requirements.lock` 等参数可额外注明与预构建镜像对应的依赖文件。完整清单本身也覆盖所有普通文件，包括锁文件与测试配置。

核对源码、预构建依赖、目标检查和失败指纹后，在 API、Worker、Runner 三端配置相同的登记内容并重启服务。例如本地 `.env`：

```dotenv
BUILDMEDIC_RUNNER_PROFILES=./var/runner-profiles.json
```

未配置或配置为空时不开放任何远程执行；登记文件无效会返回可操作的错误。仓库、SHA、workflow 路径、Job 名和 head repository 必须同时匹配。Fork 来源、未登记提交、其他 matrix Job 均不会得到执行权限。

失败指纹同时在 CI 日志与本地原始目标失败中核对。请选择能区分该缺陷和环境错误的具体文本；命令失败但指纹不匹配时返回 `not_reproduced`，不会调用模型修复。示例的 `test_pages` 仅用于开发案例，真实仓库应按自己的错误选择指纹。

## 执行与证据

1. 创建任务时将服务端匹配出的 `runner_profile` 绑定写入任务；浏览器不能传任意命令、镜像、profile 或源码路径。
2. Worker 再次核对 Run、attempt、Job、SHA 和登记内容，读取 CI 日志。
3. 内部 Runner 下载固定 SHA 的公开归档并校验登记清单。仅提供受限源码文本与对应 workflow 给 Worker，不提供 Docker socket 或宿主机文件路径。
4. Worker 将这些文本放入只用于补丁生成的任务临时目录；不导入或执行仓库 Python 文件。归档完整 profile、源码完整性清单和 workflow 证据。
5. Runner 对原始目标、基线回归、候选检查、最终独立目标与回归分别创建干净副本。远程仓库只能使用已登记的固定 Docker 镜像，可信本机模式拒绝执行它们。
6. 只有原失败复现、选中补丁指纹一致、目标与回归通过、测试身份与数量未缩水，才返回 `repaired`。该结论覆盖登记环境的本地检查，不代表远端 GitHub CI 成功。

标准模式与自适应模式共用上述执行与验收入口。原生 Harness² 仍运行 k=1 的四个 rollout、INV / DOM / COMPOSE，并由平台另外验收选中的组合补丁。远程执行没有打开新的模型工具权限。

任务详情显示登记 profile、源码清单指纹和下载链接。`source_snapshot` 包含绑定、传输归档 SHA256、清单与完整 profile 的产物 ID；产物继续检查任务会话归属。已登记任务的重试不换用新 profile，重新验证固定原提交与原补丁；配置变化时停止并保留原结果。

长日志按完整脱敏产物建立失败窗口、步骤和时间戳索引。未登记仓库的日志诊断与修复基线优先保留失败位置，归档实际模型片段及省略范围；详情页可按原始行号跳转、分页和续读超长行，见[日志证据](logs.md)。

GitHub 元数据与日志支持有次数 / 等待上限的重试，日志下载失败会重新申请官方临时链接。已完成 Job 可在会话内复用脱敏日志，仍须先重新核对固定关联；任务记录来源与内容指纹，见 [GitHub 读取与缓存](github-ingestion.md)。此日志缓存与下文的登记源码快照缓存独立。

## 接入限制

- 只从固定的 `codeload.github.com` HTTPS 地址下载完整 SHA 的公开归档，不发送 GitHub 凭据或 cookie，不跟随重定向。固定 Git 引用支持与 GitHub 的[仓库归档接口](https://docs.github.com/en/rest/repos/contents#download-a-repository-archive-tar)一致；项目在下载后另核对管理员登记的完整内容清单。
- 压缩数据最多 8 MB，展开最多 32 MB，最多 1,500 个条目，单文件最多 2 MB。拒绝路径逃逸、重复条目、多根归档、链接、稀疏文件、设备文件、`.git` 与 `.env` 路径。
- 模型源码最多 40 个 Python 文件、总计 160 KB、每个文件 20 KB；workflow 最多 20 KB。含需脱敏内容时停止，不把敏感源码送给模型，也不交付被改写的完整性清单。
- 补丁仍只允许已有 `src/**/*.py`，最多 5 个文件与 200 行变化。`src` 下的测试目录、常见测试文件和 `conftest.py` 同样只读。远程保护清单覆盖所有不可修改文件和目录，而非只覆盖 Python 测试。
- 缓存以完整绑定为键，每次读取重新核对内容；损坏时停止，不自动用其他提交替换。归档下载不执行代码，测试容器继续断网、非 root、只读并使用现有资源限额。

登记文件与镜像属于管理员配置。当前不接收任意陌生仓库，也没有仓库自动注册或 workflow 任意命令执行接口。

## 已验证与剩余验收

日常测试使用明确标注的 GitHub HTTP fixture。容器集成以真实归档中的失败源码运行标准与自适应修复及重新验证；测试模型是脚本替身，不宣称真实远端 CI 成功。另已实际下载 `duskto/BuildMedic` 的公开固定提交 `e8b153c4761513001a9fe28634a62535af5c806c`，通过归档解析和完整性清单生成；没有运行该远程仓库代码。

尚未收到用于验收的真实失败 Run URL，也没有发布新仓库或工作流。真实 Run、日志、登记镜像与模型组合的端到端验收仍待执行，因此不把本轮开发标为整个 P1 已完成。
