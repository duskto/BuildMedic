# 独立私有行为评分

评分服务在整批四组实验完成后，检查已冻结、已选中的补丁。它与公开 Runner、Worker 和模型调用分别运行；只把签名回执和汇总送回 API。评分不修改原任务结论、经验反思或公开实验报告。当前支持原三个公开开发案例和 20 项公开构造任务的完整分区，尚无真实分组模型成绩或泛化结论。

## 材料与执行边界

私有评分包必须位于项目工作区外，禁止通过符号链接引入。API 只配置评分服务地址、内部令牌和评分包指纹，不配置私有文件路径，不接收客户端提交的指标。包内保存独立调用参数、期望输出或预期异常，只有评分协调进程读取。

每次观测在固定 Docker 镜像中启动新容器，只挂载候选 `src/` 和公共调用网关。容器不挂载断言、参考答案、项目测试、评分状态目录、模型凭据或 Docker socket。调用参数通过有界 stdin 传入；期望答案留在协调进程，由其比较 JSON 输出。容器断网、非 root、只读文件系统、限 CPU / 内存 / PID / 输出与时限。没有可信本机执行的评分开关。

评分先观测原始快照，再观测选中补丁。原始观测在同一评分任务内按案例复用，候选观测各用全新容器。必须在私有行为中复现至少一个原始失败，才产生通过 / 失败结论；原始行为全部通过时记 `not_reproduced`。超时、协议输出错误或环境问题记 `evaluation_error`，不冒充行为失败。

API 要求整批完成、源码与补丁指纹一致，并绑定原报告、固定镜像、请求 ID 与评分包。HMAC 回执包含这些绑定以及评分代码指纹；API 校验签名、完整行集合和候选归属后才入库。浏览器不能自行上传评分回执。每份实验保留一个评分请求；断网后恢复同一请求，不更换评分包重跑。评分进程重启会把未完成的观测记为未完成，保留回执而非伪造成功。

评分表不参与模型材料、工具索引、策略提议、候选选择或经验反思。详情页另列独立评分；原公开报告与其 hash 保持不变。签名令牌仅用于 API 与评分服务通信，不提供给浏览器或候选容器。

本机开发验收是不同进程、外部私有目录与不同容器挂载的隔离。不同主机 / 账号之间的权限、运维和故障隔离还需部署验收；生产配置应把评分服务及私有存储放在独立受信任节点。普通 Docker 的适用范围仍是登记案例。

## 操作

从外部受控位置准备 JSON 包，结构字段如下。不要将实际断言复制进 Git、模型提示词、镜像构建上下文或公开产物。

| 字段 | 要求 |
| --- | --- |
| `schema_version` / `suite_id` / `version` | 原三案例包为 `1.0`，明确名称与正整数版本；分组包为 `2.0`，见下文 |
| `dataset` | 原三案例使用 `public-development` |
| `development_groups` / `test_groups` | 组名列表，内部去重且两侧不得重叠 |
| `cases` | 最多三个登记案例，包含 `case_id`、`group`、`snapshot_sha256`、`probes` |
| `group` | 当前公开案例必须属于开发组，拒绝将其标成隔离测试组 |
| `probes` | 每案例 1–50 项；每项有登记的 `entrypoint`、JSON `args` / `kwargs`，以及 `expected` 或 `raises` |
| `entrypoint` | 分页 `paginate`；发票 `invoice_total`；运费 `shipping_cost`、`checkout`、`quote_order` |
| `raises` | 可选 `ValueError` / `TypeError`；没有时比较 JSON 值 |

操作员在评分节点校验包，只输出指纹：

```sh
uv run --extra adaptive python scripts/check_scoring_bundle.py /srv/buildmedic-private/bundle.json
```

评分节点设置独立环境变量，不使用 API 的模型密钥：

```dotenv
BUILDMEDIC_SCORER_BUNDLE_PATH=/srv/buildmedic-private/bundle.json
BUILDMEDIC_SCORER_STATE_DIR=/srv/buildmedic-private/state
BUILDMEDIC_SCORER_TOKEN=your-long-internal-token
BUILDMEDIC_SCORER_IMAGE=sha256:your-reviewed-fixed-image-id
```

状态目录也必须在工作区外，权限由服务限制。评分节点需要 Docker CLI / daemon 及已登记公共案例的源码副本。启动服务：

```sh
uv run --extra adaptive uvicorn scorer_service.app:create_app --factory --host 127.0.0.1 --port 8002
```

API / Worker 的 `.env` 仅增加：

```dotenv
BUILDMEDIC_SCORER_URL=http://127.0.0.1:8002
BUILDMEDIC_SCORER_TOKEN=the-same-internal-token
BUILDMEDIC_SCORER_SUITE_SHA256=the-validated-bundle-fingerprint
BUILDMEDIC_SCORER_DATASET=public-development
```

地址只接受 HTTPS 或本机 HTTP，禁止重定向。镜像必须与被冻结实验的固定 Docker 镜像一致。设置后重启 API，在已完成四组清单详情页点击“运行独立行为评测”，或复用原私有会话文件：

```sh
uv run --extra adaptive python scripts/run_scoring.py --state var/acceptance/four-arm-session.json
```

这个命令不请求模型。回执写入同目录 `*-score.json`，维持 0600 权限。临时服务不可用时保持原请求，可重用命令恢复；签名或归属错误会拒绝回执，不生成分数。私有服务状态与包不应放进 API / Worker 的部署挂载。

## 指标与可见信息

公开报告中的 `hidden_verified=null` 保持原值；独立结果在单独评分表及接口中保存。

| 指标 | 分子 / 分母 |
| --- | --- |
| 行为与公开回归通过 | 私有行为通过且原公开独立验收通过 / 预先登记的该修复组任务数 |
| 误报修复 | 公开声称修复、私有行为失败 / 已完成有效私有评分的公开修复数 |
| 新增行为回归 | 补丁使原来通过的私有行为失败 / 有原先通过行为可比较的已评分补丁数 |
| 评分覆盖 | 有有效行为通过 / 失败结论的任务数，另列环境异常、未复现、无候选 |

原始私有行为全部失败时，可以检查修复，但没有原先通过行为可评估新增回归；该项不计入新增回归分母。无候选仍留在修复率总分母中。日志组不参与修复率；诊断准确率保持未评分。分母为零表示不可计算。回执只包含计数、状态、候选 / 来源 / 镜像 / 代码指纹，不返回参数、期望值、原始输出、私有路径或异常详情。

API：`POST /api/v1/experiments/{id}/score` 启动或恢复同一请求，`GET` 同路径查询，`GET .../score/report` 下载。全部校验会话归属；POST 不接受请求体。内部 `/scorer/v1/*` 要求独立 Bearer token，不能直接供浏览器调用。数据库迁移 `0004` 仅增加评分表。

## 验收范围

新增分组协议的自动化验收使用明确公开的合成向量，运行 20 项原始源码及显式 QA 修复的真实 Docker 观测，覆盖共享默认参数与事务回滚。完整 HTTP 闭环分别提交开发 / 测试分区的 40 行请求，校验 80 行签名结果与分母；这只证明评分链路和 QA 修复可执行，不是模型成绩，也不构成生产私有题库。另证明通过公开回归的分组过拟合补丁可被独立输入判为失败，并检测新增行为回归。

CI 中的私有包是测试期间在临时目录生成的明确替身，不是实际生产私有材料。真实 Docker 检查证明：一个通过公开回归的过拟合补丁，会在独立输入下被判为误报并出现新增回归；断言和期望值没有传入候选容器。

开发验收使用外部包对已有四组实验 `ba1b112388f2407b88c3521d59783172` 评分，没有新增模型调用。9 个选中补丁均通过独立行为与公开回归，评分异常为零，日志组不参与此行为评分。该结果只覆盖熟悉的开发案例，尚未建立 20–30 个真实分组隔离任务，也没有真实诊断准确率或泛化收益结论。诊断审核另见[离线人工审核流程](diagnosis-review.md)，与本行为回执独立。

## 20 项分组任务的评分包

公开实验协议 3.0 对应评分请求、回执及私有包的 2.0 协议；原实验 2.0 对应评分 1.0，保持原请求字节与签名口径。分组包包含：

| 字段 | 要求 |
| --- | --- |
| `schema_version` | `2.0` |
| `suite_id` / `version` | 明确名称与正整数版本 |
| `dataset` | `public-constructed-grouped` |
| `dataset_manifest` | 复制固定的完整 20 项公开清单 `evaluation/grouped-dataset.json` |
| `dataset_sha256` | 完整清单规范化指纹，与实验冻结值一致 |
| `cases` | 覆盖清单的每个案例，无遗漏或重复；每项含 `case_id`、`group`、`snapshot_sha256`、`probes` |
| `group` | 与公开清单的 `template_group` 一致，不能重标开发 / 测试关系 |
| `probes` | 每项 1–50 个独立输入，结构与 1.0 相同；仅允许固定登记入口 |

只把格式和公开身份复制到外部包，实际输入与期望答案由可信数据管理员独立维护。不要用 `tests/support/synthetic_probes.json` 冒充私有评分包；它是已公开的测试替身，用于验证流程，无隐藏性。该文件不进入 Solver / Proposer 材料。

除原三案例入口外，新增入口为：

| 案例 | 固定入口 |
| --- | --- |
| mutable-default | `append_value` |
| csv-quoting | `encode_row` |
| merge-precedence | `merge_config` |
| stable-dedup | `unique` |
| fullmatch | `valid_identifier` |
| clamp | `clamp` |
| weighted-mean | `weighted_mean` |
| graph-cycle | `has_cycle` |
| leap-year | `is_leap` |
| utf8-size | `fits_utf8` |
| version-order | `sorted_versions` |
| missing-sentinel | `value_or_default` |
| boolean-parser | `parse_boolean` |
| zip-length | `pair_records` |
| rollback | `rollback_mapping` |
| fraction-sign | `reduce_fraction` |
| path-containment | `is_within` |

每次容器观测按给定顺序执行同一案例的 probes，同一模块实例保留调用间状态；每条结果会在下一次调用前复制为 JSON 值，因此能检测默认参数污染，同时避免后续修改覆盖先前观测。不同候选、原始源码及其他案例均使用新容器。

`rollback_mapping` 是固定网关场景，不接受代码或回调：三个位置参数分别为初始映射、更新映射、待删除键列表。网关在候选 `transaction` 上下文中执行更新和删除，再抛出受控异常，返回异常后的映射供协调器比较。期望答案仍留在容器外。结果不可序列化或出现非有限浮点数会记录观测错误，不能当作函数抛出了预期 `ValueError` / `TypeError` 获得通过。

用同一 `scripts/check_scoring_bundle.py` 校验外部 2.0 包并取得指纹；失败只给出通用错误，不打印私有输入、期望值或 traceback。包文件上限 600 KB，分组评分请求上限 24 MB / 360 行，API 接收回执上限 512 KB。HTTP 请求绑定完整分区、重复轮次、轮换顺序、来源及候选，拒绝删项、重排、跨分区或指纹不一致。

评分节点配置方式相同，API 另选择对应数据集：

```dotenv
BUILDMEDIC_SCORER_DATASET=public-constructed-grouped
```

在评分节点使用新的外部包，更新 API 的包指纹与服务配置后，再从页面或 `scripts/run_scoring.py` 启动评分。页面和 API 会提前拒绝数据集配置不匹配，不创建误投的评分记录。待处理请求绑定原服务、包和数据集；切换配置后需恢复原配置才能继续。已封存的历史回执继续可读，无须重新运行模型或改写原报告。
