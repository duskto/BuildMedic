# Runner 退出原因与清理确认

测试执行结果同时记录测试事实和执行节点事实。测试断言失败、内存耗尽、超时和清理失败分别展示；资源故障不作为已复现的业务缺陷，不会继续调用模型修复。

## 容器执行流程

Runner 使用固定参数 `docker create` 创建容器，再执行 `docker start --attach`。先创建再启动，使创建期间收到取消时可以直接移除容器而不启动测试。创建、状态读取、kill 和删除的单次 Docker 管理调用各有 10 秒上限；用户和模型不能提交这些命令或修改限额。

容器不再使用自动 `--rm`，而是在执行后通过 Docker [container inspect](https://docs.docker.com/reference/cli/docker/container/inspect/) 读取退出状态，然后显式删除。容器带有 Runner 根目录指纹、execution ID 和请求指纹标签；只有匹配的容器才能被 kill / 删除，实际操作使用不可变容器 ID。旧版未带标签的执行仅按已有本地执行记录兼容恢复。Docker daemon 的内部报错和完整 inspect 数据不会返回给浏览器。

默认限制为 180 秒、2,000,000 字节 stdout 监测上限、1 GiB 内存、1 CPU、128 PID。`--memory-swap` 与 `--memory` 同为 1 GiB，按 Docker 的[内存限制规则](https://docs.docker.com/engine/containers/resource_constraints/) 禁止容器使用额外 swap。Docker 使用 `none` 日志驱动避免另存一份无界 daemon 日志；attach 输出仍由 Runner 采集。保留断网、非 root、只读源码和根文件系统、禁用提权等既有限制。

stdout 在进程运行中检查，并在退出后再次检查，防止快速退出的超量输出漏过限制。监测并非文件系统硬配额，轮询期间可能暂时超出阈值；归档最多读取设定字节数，再以 UTF-8 替换非法字节并脱敏。磁盘总配额与历史产物保留期仍需另行完善。

## 退出原因

| `reason` | 含义 |
| --- | --- |
| `exited` | 正常取得退出状态；测试是否通过仍看退出码、JUnit 及完整性校验 |
| `oom_killed` | Docker 明确报告 `OOMKilled=true`；即使其他测试结果看似通过，也不会判为成功 |
| `timeout` | 达到执行时限，平台终止执行 |
| `output_limit` | 日志超过大小上限 |
| `user_cancelled` | 收到用户取消意图；与超时、输出超限按最先记录的原因区分 |
| `signal` | 可信本机模式从 waitpid 的负返回值确认信号 |
| `abnormal_exit` | Docker 返回可能对应信号的异常退出码，不能仅凭该码确认具体信号或 OOM |
| `runtime_error` | 容器创建、启动或最终执行状态不一致 |
| `container_state_unavailable` | 无法确认容器最终状态，不能作为通过证据 |
| `cleanup_unconfirmed` | 容器、进程或工作目录清理尚未确认；执行生命周期为 `interrupted` |
| `runner_restarted` | Runner 重启前尚未结束的执行，恢复时停止并清理，不自动重跑 |

例如显式 `exit(137)` 和外部 SIGKILL 都可能得到 137，但只有 Docker 的 OOM 标记为真才报告内存耗尽。`termination.signal` 保存已确认的信号，`possible_signal` 仅保存异常退出码可能对应的信号，页面明确标为未确认。

每个结果的可选 `termination` 包含原始 `cause`、Docker CLI / 容器退出码、OOM 标记、信号、容器状态、状态确认及清理确认字段；`resource_limits` 记录实际固定限额并参与环境指纹。字段合同见 `packages/contracts/execution-termination.schema.json`。旧任务没有这些字段时继续兼容读取，不补造历史状态。

## 清理与恢复

无论测试通过、失败、取消或异常，Runner 都清理本地进程组、移除自己拥有的容器并删除本次工作目录。只有清理结束后才更新公开终态及持久记录；正在清理时仍显示运行中。清理未确认时强制 `passed=false`、`lifecycle=interrupted`，保留原始原因及清理错误类别。

启动时重新检查此前未完成或清理未确认的记录。Docker 不可用、身份标签不符或目录删除失败时不会假装成功，health 返回未就绪并阻止后续负载执行。恢复 Docker 或目录权限后，管理员可通过带 Runner Bearer 认证的既有接口重试清理：

```text
POST /runner/v1/executions/{execution_id}/cancel
```

该操作保持原执行中断状态，只更新清理确认；不重跑测试或模型。也可以重启 Runner 触发恢复。原有 OOM、退出码与故障原因在恢复时保留，页面会区分“清理重试已完成”和原始故障。清理完成前应保留原 Runner 数据目录位置和 backend 配置，避免身份指纹变化。可信本机模式不按持久化 PID 猜测并杀死重启前的进程；无法确认时保持阻塞，需要管理员检查原进程。它仍只用于可信开发案例，不提供容器隔离。

Worker 在取消后读取并归档 Runner 最终结果及脱敏日志。清理失败的执行继续保留在待清理集合，重试清理仍失败时任务标记中断，不报告“取消已完成”。独立验收另外拒绝状态或清理未确认、发生 OOM 却声称通过的结果。详情页在执行日志和验证卡片展示退出原因、信号、限额与清理状态。

## 验证范围

真实固定镜像检查使用明确合成的资源故障案例，覆盖 128 MiB 内存限制下的 OOM、显式退出 137、外部 Docker SIGKILL、超时、输出超限、用户取消，以及移除容器后仍能读取持久退出事实。本机进程检查覆盖实际 SIGTERM、创建期间取消、清理前不发布终态、目录清理失败及取消后的 Worker 归档。

Docker 服务不可用、身份不匹配、删除失败与恢复重试使用管理接口替身验证；它们不等价于多节点 Docker 故障部署验收，也不替代真实 GitHub 失败 Run 验收。
