# 独立诊断审核与分组清单

四组实验可在结束后导出离线人工审核单，审核根因与定位是否正确，再导入签名回执。主服务、Worker、Solver 和 Proposer 不加载私有参考答案；已有公开报告、诊断和补丁不会被评分改写。此流程不调用模型或 Runner。

目前实现并验证的是审核协议和完整离线流程，测试使用明确标记的合成判断。历史真实实验尚无独立人工审核，诊断准确率仍未评分。不能把自动化测试、审核单导出或模型自评当成真实准确率。

## 审核口径

- `root_cause_correct`：诊断准确说明导致故障的具体机制。仅重复报错、泛泛建议或碰巧给出正确补丁不足以判正确。
- `location_correct`：诊断准确指出有关代码位置、模块或调用关系，具体到该案例私有规则要求的粒度。逐案例先冻结规则，不能看到组别或修复结果后调整标准。
- `diagnosis_accuracy` = 两项同时正确的诊断 / 该组全部计划任务。失败、取消、预算耗尽或缺失诊断不从分母中移除；缺失诊断计零。
- 已交付但未审核的诊断不自动判错或判对。整份审核未封存前，所有组准确率均为 `null`。回执要求所有已交付诊断的两项均为布尔判断，缺失诊断的两项必须为 `null`。
- 同时展示计划、交付、缺失、已审核、未审核和各项正确数。日志组可以参与诊断审核，仍不参与修复率。

每份完成的四组实验最多保留一份不可覆盖的审核。请求固定原报告、源码、诊断及引用证据的指纹、随机审核 ID 和规则指纹。修订规则或重新评判需要新实验；本版不支持覆盖既有判断。旧版两组实验不参与此流程。

## 私有参考规则

独立审核人或数据管理员在工作区外维护规则，例如 `/srv/buildmedic-review/rubric.json`。文件包含以下字段：

| 字段 | 内容 |
| --- | --- |
| `protocol` | `diagnosis-rubric-v1` |
| `version` | 正整数版本 |
| `dataset` | 完整数据集清单；开发集可复制 `evaluation/dataset-development.json` 的内容 |
| `references` | 每个案例一项，含 `case_id`、`root_cause`、`location`、`guidance` |

`root_cause` 填独立确认的故障机制，`location` 填位置及允许的定位粒度，`guidance` 填边界情况、等价表述和判定规则，均为非空文本。必须覆盖清单的每个案例且不重复；源码指纹必须与审核请求一致。规则、包含规则的审核单和备注须放在工作区外；工具拒绝工作区路径、符号链接和超限文件。

审核单仅投影诊断及其引用证据的类型、路径、原始行号与截取内容。单条证据最多 600 UTF-8 字节，并标明截断；审核人可按私有规则查看固定源码。不提供补丁、容器结果、调用用量、模型、任务 ID、组别或缓存元数据。审核 ID 随机生成，顺序打乱，组别关联仅存在主服务数据库。

这是对审核单字段的盲化；诊断正文或证据本身可能透露策略线索，不能声称严格双盲。签名证明回执来自持有密钥的可信操作者，不能自动证明判断正确、审核人独立或进行了人工审核。正式研究应安排独立审核人，保留私有判断和分歧处理记录。

## 操作流程

先迁移数据库，主服务操作者配置两个默认空值的环境变量：

```sh
uv run --extra adaptive python scripts/migrate.py
export BUILDMEDIC_DIAGNOSIS_REVIEW_KEY='<至少 32 字符的独立随机密钥>'
uv run --extra adaptive python -m scorer_service.diagnosis_review rubric-hash \
  --rubric /srv/buildmedic-review/rubric.json
export BUILDMEDIC_DIAGNOSIS_RUBRIC_SHA256='<上一步的 64 位指纹>'
```

尖括号值需要替换。密钥可保存在被忽略的 `.env`，通过私有渠道交给封存操作者，不要交给模型或放入审核单；也不要复用行为评分服务令牌。

主服务数据库操作者导出请求。CLI 使用本机数据库权限读取归属，无须网页会话 cookie：

```sh
uv run --extra adaptive python -m buildmedic.diagnosis_cli prepare \
  --experiment '<已完成的四组实验 ID>' \
  --file /srv/buildmedic-review/request.json
```

移交请求给审核操作者，在工作区外的审核环境生成审核单：

```sh
uv run --extra adaptive python -m scorer_service.diagnosis_review export \
  --request /srv/buildmedic-review/request.json \
  --rubric /srv/buildmedic-review/rubric.json \
  --output /srv/buildmedic-review/sheet.json
```

只将 `sheet.json` 交给独立审核人。逐项填写 `root_cause_correct`、`location_correct`，可填写私有 `notes`；全部判断结束后填写非空 `reviewer` 标识，将 `human_review_complete` 设为 `true`。缺失诊断保留两个 `null`，不要修改 `item`、`reference`、审核 ID 或指纹。

可信操作者检查人工审核后，通过环境变量提供同一密钥并封存回执：

```sh
uv run --extra adaptive python -m scorer_service.diagnosis_review seal \
  --request /srv/buildmedic-review/request.json \
  --rubric /srv/buildmedic-review/rubric.json \
  --sheet /srv/buildmedic-review/sheet.json \
  --output /srv/buildmedic-review/receipt.json
```

封存核对全部材料与判断。签名回执不含参考答案、备注或审核人原始标识，仅含判断、审核人标识 hash 和绑定指纹。输出原子发布、权限 0600，不覆盖已有不同文件。重复封存会产生新时间戳，请使用新路径；导入同一回执可安全重复。

主服务操作者仅导入回执：

```sh
uv run --extra adaptive python -m buildmedic.diagnosis_cli import \
  --experiment '<同一实验 ID>' \
  --file /srv/buildmedic-review/receipt.json
```

页面“独立诊断审核”中刷新即可查看和下载。更换密钥不会改变已封存结果；未导入回执按当前密钥校验。已准备请求始终保留原规则指纹，修改配置不会替换它。

只读 API `GET /api/v1/experiments/{id}/diagnosis-score` 返回当前会话的独立审核或 `null`，追加 `/report` 下载状态、汇总和回执。其他会话返回 404。没有公网提交判断或导入规则的接口。下载不含原审核单或参考规则；原公开报告的 `diagnosis_accuracy` 仍为 `null`，人工判断只在独立记录中提供。

## 分组清单预检

```sh
uv run --extra adaptive python -m buildmedic.evaluation_dataset evaluation/dataset-development.json
```

公开清单只含案例 ID、仓库组、缺陷模板组、开发 / 测试划分、源码和日志 SHA-256，不含答案。`public-development` 不允许测试项；`grouped-benchmark` 要求 20–30 项且同时有两种划分。仓库组、缺陷模板组、源码和日志指纹均不能跨划分重叠。三个已公开案例的 ID、源码或日志即使换名也不能被重新标为测试样本。

工具返回规范化清单指纹及数量；其中 `execution_registered=false` 表示预检本身不登记执行。这只检查提供的元数据，组标签是否真实仍需管理员审核，不能发现未知的语义近重复。另有 [20 项公开构造任务](grouped-evaluation.md)通过 3.0 协议接入完整分区执行与审核，最多 360 行判断；预检通过不能替代执行登记。严格隔离的真实任务、正式测试经验快照、真实诊断准确率和泛化收益结论仍待完成。
