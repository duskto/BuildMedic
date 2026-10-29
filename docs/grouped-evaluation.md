# 20 项公开构造任务的分组对照

`schema_version=3.0` 接入 20 个公开小型任务，分为 10 项开发、10 项测试。每个分区运行日志解释、单次固定策略、独立多次固定策略和 Harness² 四组，共 40 项执行；支持预登记 1–3 次重复。经验库固定关闭，三个修复组使用相同工具边界与总预算。

这些是明确构造、完整公开的 Python 项目，不是 20 个真实 GitHub 故障或未公开盲测案例。“仓库组”表示独立构造项目目录；原三个演示案例共享开发组。划分保证声明的项目组和缺陷模板组不跨分区，不能自动证明没有语义近似或训练集污染。分组任务的可执行性验收不等于模型修复效果。

## 任务范围

| 分区 | 案例 | 缺陷模板 |
| --- | --- | --- |
| 开发 | pagination、imports、signature | 原三个演示任务：切片边界、内部模块导入、调用参数传播 |
| 开发 | mutable-default | 共享默认参数导致跨调用污染 |
| 开发 | csv-quoting | CSV 字段转义策略 |
| 开发 | merge-precedence | 配置覆盖方向 |
| 开发 | stable-dedup | 保留首次出现顺序的去重 |
| 开发 | fullmatch | 正则表达式验证完整输入 |
| 开发 | clamp | 上下界裁剪组合 |
| 开发 | weighted-mean | 加权平均分母 |
| 测试 | graph-cycle | 有向图递归栈环检测 |
| 测试 | leap-year | 公历世纪闰年规则 |
| 测试 | utf8-size | 字符与 UTF-8 字节单位 |
| 测试 | version-order | 版本数字分量比较 |
| 测试 | missing-sentinel | 缺失键与有效空值区分 |
| 测试 | boolean-parser | 布尔文本解析 |
| 测试 | zip-length | 批量输入长度校验 |
| 测试 | rollback | 异常后的状态原子性 |
| 测试 | fraction-sign | 约分后的分母符号 |
| 测试 | path-containment | 路径组件关系与字符串前缀区分 |

源码与公开测试位于 `fixtures/public_cases/{case_id}`。`tests/support/grouped_repairs.json` 是明确公开的 QA 修复替身，只用于验证任务可复现和平台检查可执行；不会作为模型诊断材料。不得将其执行结果报告为模型生成的修复或隐藏行为成绩。

新增 17 个任务的失败日志实际采自固定 Docker 镜像的目标检查，归档在 `evaluation/public_logs`，包含来源、源码、镜像和日志指纹；不是 GitHub 日志。登记要求 Runner 为 Docker，镜像与所选分区的日志完全一致。当前归档镜像：

```text
sha256:23449681af91bd411cb891effde770f0b081af16472495ef3423b9800381535e
```

## 登记与运行

页面“策略对比”的“实验组别”中选择“分组构造开发集”或“分组构造测试集”。登记只保存清单；查看固定设置后，点击运行才开始请求模型。正式测试前应先在开发分区完成参数选择，然后固定提示词、预算、工具设置和空经验输入。测试分区全部公开，研究者须自行记录接触和调参历史，不能声称从未看过的盲测。

也可使用已有 CLI：

```sh
uv run --extra adaptive python scripts/dev.py
# 以下运行会产生真实模型用量，每个分区至少计划 40 项任务。
uv run --extra adaptive python scripts/run_evaluation.py \
  --plan evaluation/grouped-development.json \
  --state var/acceptance/grouped-development-session.json
# 完成开发并冻结参数后，再独立运行测试分区。
uv run --extra adaptive python scripts/run_evaluation.py \
  --plan evaluation/grouped-test.json \
  --state var/acceptance/grouped-test-session.json
```

会话状态含私有 cookie，保留在忽略目录。运行中断后重用相同状态文件。协议仍按顺序安排任务，使用原有配额、outbox、Worker 租约和取消机制。

## 固定边界

`evaluation/grouped-dataset.json` 保存完整 20 项分组清单；两份运行计划均嵌入该清单和相同数据集指纹：

```text
02c624e0c357a2fdcfe041e2b1696bf6905b47f49ac32a7290a2805de6fa601c
```

每份计划还记录当前分区的全部案例、源码 / 日志指纹、修复预算、多次尝试数、重复轮次和工具开关。服务端仅接受登记的数据集与完整分区，拒绝删掉困难案例、重排选择顺序、混合分区、修改源码 / 日志或提交任意命令。创建和安排后续任务时重新核对当前登记内容。

任务快照的 `experiment_binding` 增加数据集指纹、分区、仓库组与缺陷模板组。报告校验任务身份、预登记顺序、组别、日志、源码和镜像绑定，缺失或未执行项留在总分母。旧版 1.0 / 2.0 清单和报告复算保持兼容。

`GET /api/v1/evaluation/grouped-plans` 返回两份当前完整计划，不执行模型。报告、下载、取消、续跑及描述性配对分析使用已有实验 API；实际实验仍检查会话归属。运行时配置或策略变动会停止安排后续任务。

离线复算使用导出的冻结清单与输入，不读取当前案例源码、登记目录或网络：

```sh
uv run --extra adaptive python scripts/recompute_evaluation.py '<导出的报告路径>'
uv run --extra adaptive python scripts/analyze_evaluation.py '<导出的报告路径>'
```

分组任务支持独立[人工诊断审核](diagnosis-review.md)，最多 360 行签名判断；旧请求保持兼容。本版尚未提供这些新增任务的独立私有行为包，页面明确展示未评分，不调用原三个案例的行为评分服务充数。

## 当前验证结果

新增 17 项原始故障均已在固定镜像中复现，显式 QA 补丁的目标与回归通过。自动化回归另对每个任务执行原始目标、原始回归、修复目标、修复回归四个独立检查，核对测试身份、源码与测试指纹、容器清理和平台验证规则；新增 17 项 Docker 测试通过，共 68 次容器执行。初始日志采集另有 51 次检查，证据位于忽略目录 `var/qa/grouped-docker-acceptance.json`。

当前没有运行整批付费模型对照，没有新增真实人工标签或独立私有行为成绩。真实模型的分区成绩、经验迁移收益、独立行为包和严格盲测仍待完成。
