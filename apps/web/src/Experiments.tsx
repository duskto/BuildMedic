import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";
import { api } from "./api";
import { healthSchema } from "./contracts";
import { IndependentScore } from "./IndependentScore";

const summarySchema = z.object({
  mode: z.enum(["logs", "standard", "multi", "adaptive"]),
  planned: z.number(),
  finished: z.number(),
  public_verified: z.number(),
  reproduced: z.number(),
  calls: z.number(),
  tokens: z.number(),
  unknown_usage: z.number(),
  cost_usd: z.number().nullable(),
  cost_per_public_verified: z.number().nullable(),
  tokens_per_public_verified: z.number().nullable(),
  wall_seconds: z.number(),
  checks: z.number(),
  rollouts: z.number(),
  blocked: z.number(),
  budget_exhausted: z.number(),
  interrupted: z.number(),
  repair_applicable: z.boolean().optional(),
  diagnoses: z.number().optional(),
  diagnosis_accuracy: z.number().nullable().optional(),
});
export const experimentSchema = z.object({
  id: z.string(),
  created_at: z.number(),
  status: z.enum(["planned", "running", "completed", "cancelled"]),
  manifest: z.object({
    schema_version: z.string().optional(),
    multi_attempts: z.number().optional(),
    name: z.string(),
    dataset: z.literal("public-development"),
    repeats: z.number(),
    cases: z.array(
      z.object({
        case_id: z.string(),
        snapshot_sha256: z.string(),
        log_sha256: z.string().optional(),
      }),
    ),
    agent_tools: z.boolean(),
    limits: z.object({
      max_model_calls: z.number(),
      max_tokens: z.number(),
      max_wall_seconds: z.number(),
    }),
  }),
  manifest_sha256: z.string(),
  configuration_sha256: z.string(),
  report_sha256: z.string(),
  configuration: z.object({
    model_id: z.string(),
    provider: z.string(),
    harness2_sha: z.string(),
  }),
  scope: z.string(),
  environment_consistent: z.boolean().nullable(),
  finished: z.number(),
  planned: z.number(),
  summary: z.array(summarySchema),
  rows: z.array(
    z.object({
      case_id: z.string(),
      repeat: z.number(),
      mode: z.string(),
      task_id: z.string().nullable(),
      metrics: z
        .object({
          public_verified: z.boolean(),
          lifecycle: z.string(),
          outcome: z.string().nullable(),
          calls: z.number(),
          tokens: z.number(),
          usage_known: z.boolean(),
          wall_seconds: z.number(),
          checks: z.number(),
        })
        .passthrough()
        .nullable(),
    }),
  ),
});
const pageSchema = z.object({
  items: z.array(experimentSchema),
  total: z.number(),
  page: z.number(),
  page_size: z.number(),
});
const modeName = (mode: string) =>
  ({
    logs: "日志解释",
    standard: "固定策略",
    multi: "固定策略 · 独立多次",
    adaptive: "Harness²",
  })[mode] ?? mode;
const stateName: Record<string, string> = {
  planned: "已登记",
  running: "执行中",
  completed: "已完成",
  cancelled: "已停止",
};
const outcomeName: Record<string, string> = {
  diagnosed_only: "仅交付诊断",
  unresolved: "未解决",
  blocked: "环境阻塞",
  not_reproduced: "未复现",
  budget_exhausted: "预算耗尽",
  failed: "执行失败",
  interrupted: "执行中断",
  cancelled: "已取消",
  completed: "已完成",
};
function Failure({ error }: { error: unknown }) {
  return error ? (
    <p role="alert" className="notice warning">
      {error instanceof Error ? error.message : "请求失败"}
    </p>
  ) : null;
}
function money(cost: number | null) {
  return cost === null ? "未知" : `$${cost.toFixed(4)}`;
}

export function Experiments() {
  const [page, setPage] = useState(1);
  const [repeats, setRepeats] = useState(1);
  const [protocol, setProtocol] = useState("2.0");
  const key = useRef(crypto.randomUUID());
  const navigate = useNavigate();
  const list = useQuery({
    queryKey: ["experiments", page],
    queryFn: () => api(`/experiments?page=${page}&page_size=10`, pageSchema),
  });
  const cases = useQuery({
    queryKey: ["evaluation-cases"],
    queryFn: () =>
      api(
        "/evaluation/cases",
        z.array(
          z.object({
            case_id: z.string(),
            snapshot_sha256: z.string(),
            log_sha256: z.string(),
          }),
        ),
      ),
  });
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => api("/health", healthSchema),
  });
  const create = useMutation({
    mutationFn: () =>
      api("/experiments", experimentSchema, {
        method: "POST",
        headers: { "Idempotency-Key": key.current },
        body: JSON.stringify({
          name: `development-${new Date().toISOString().slice(0, 10)}`,
          repeats,
          schema_version: protocol,
          ...(protocol === "2.0" ? { multi_attempts: 4 } : {}),
          cases: cases.data
            ?.filter((c) =>
              ["pagination", "imports", "signature"].includes(c.case_id),
            )
            .map((c) => ({
              case_id: c.case_id,
              snapshot_sha256: c.snapshot_sha256,
              ...(protocol === "2.0" ? { log_sha256: c.log_sha256 } : {}),
            })),
          limits: {
            max_wall_seconds: 600,
            max_model_calls: 12,
            max_tokens: 100000,
          },
        }),
      }),
    onSuccess: (data) => navigate(`/experiments/${data.id}`),
  });
  return (
    <>
      <div className="page-title">
        <div>
          <div className="eyebrow">EVIDENCE BEFORE METRICS</div>
          <h1>策略对比</h1>
          <p>固定公开开发集，比较相同预算下的执行结果与实际消耗。</p>
        </div>
      </div>
      <div className="notice info">
        这里记录公开测试通过数。隐藏行为检查、诊断准确率和跨仓库泛化尚未评测。
      </div>
      <section className="panel padded">
        <h2>登记开发集对照</h2>
        <p>
          分页、内部导入、调用签名三个案例。四组分别为仅日志解释、单次固定策略、四次独立固定策略与
          Harness²；修复组使用相同模型、工具设置和总预算。每项最多 12
          次调用、100,000 Token、10
          分钟，经验库关闭。日志组不执行代码，诊断准确率尚未评分。
        </p>
        <label>
          实验组别{" "}
          <select
            aria-label="实验组别"
            value={protocol}
            onChange={(e) => {
              setProtocol(e.target.value);
              key.current = crypto.randomUUID();
            }}
          >
            <option value="2.0">四组对照</option>
            <option value="1.0">旧版两组对照</option>
          </select>
        </label>{" "}
        {protocol === "1.0" && (
          <p className="small-note">
            旧版固定策略最多两次带反馈修正；不等同于四组实验的单次固定策略。
          </p>
        )}
        <label>
          重复次数{" "}
          <select
            aria-label="重复次数"
            value={repeats}
            onChange={(e) => {
              setRepeats(Number(e.target.value));
              key.current = crypto.randomUUID();
            }}
          >
            {[1, 2, 3].map((n) => (
              <option key={n} value={n}>
                {n} 次
              </option>
            ))}
          </select>
        </label>
        <p>
          共 {(protocol === "2.0" ? 12 : 6) * repeats}{" "}
          项执行。登记清单后可查看预算，再启动运行；会产生真实模型用量。
        </p>
        <Failure error={cases.error ?? health.error ?? create.error} />
        <button
          className="button primary"
          disabled={
            !cases.data || !health.data?.adaptive_ready || create.isPending
          }
          onClick={() => create.mutate()}
        >
          登记对照清单
        </button>
        {!health.data?.adaptive_ready && (
          <p className="small-note">请先完成模型、Runner 与 Harness² 配置。</p>
        )}
      </section>
      <section className="panel padded">
        <h2>当前会话的对照记录</h2>
        <Failure error={list.error} />
        {list.isPending ? (
          <p role="status">正在读取…</p>
        ) : list.data?.items.length === 0 ? (
          <p>
            尚无离线对照记录。登记后会保留完整任务清单，失败和中断也计入总数。
          </p>
        ) : (
          list.data?.items.map((item) => (
            <article className="rollout-card" key={item.id}>
              <h3>
                <Link className="text-link" to={`/experiments/${item.id}`}>
                  {item.manifest.name}
                </Link>
              </h3>
              <p>
                {stateName[item.status]} · 已归档 {item.finished} /{" "}
                {item.planned} 项 ·{" "}
                {item.configuration.model_id || "未配置模型"}
              </p>
              <p className="small-note">
                {item.summary
                  .map((s) =>
                    s.repair_applicable === false
                      ? `${modeName(s.mode)}：${s.diagnoses}/${s.planned} 份诊断，未评分`
                      : `${modeName(s.mode)}：${s.public_verified}/${s.planned} 通过`,
                  )
                  .join("；")}
              </p>
            </article>
          ))
        )}
        {list.data && list.data.total > 10 && (
          <div className="pagination">
            <button disabled={page === 1} onClick={() => setPage(page - 1)}>
              上一页
            </button>
            <span>
              {page} / {Math.ceil(list.data.total / 10)}
            </span>
            <button
              disabled={page * 10 >= list.data.total}
              onClick={() => setPage(page + 1)}
            >
              下一页
            </button>
          </div>
        )}
      </section>
    </>
  );
}

export function ExperimentDetail() {
  const { id } = useParams();
  const cache = useQueryClient();
  const [running, setRunning] = useState(false);
  useEffect(() => setRunning(false), [id]);
  const detail = useQuery({
    queryKey: ["experiment", id],
    queryFn: () => api(`/experiments/${id}`, experimentSchema),
    refetchInterval: 3000,
  });
  const advance = useMutation({
    mutationFn: () =>
      api(
        `/experiments/${id}/advance`,
        z.object({ status: z.string(), task_id: z.string().nullable() }),
        { method: "POST" },
      ),
    onSuccess: (data) => {
      if (["completed", "cancelled"].includes(data.status)) setRunning(false);
      void cache.invalidateQueries({ queryKey: ["experiment", id] });
      void cache.invalidateQueries({ queryKey: ["experiments"] });
    },
    onError: () => setRunning(false),
  });
  useEffect(() => {
    if (!running || advance.isPending) return;
    const timer = setTimeout(() => advance.mutate(), 1500);
    return () => clearTimeout(timer);
  }, [running, advance.isPending, advance.submittedAt, advance.mutate]);
  const cancel = useMutation({
    mutationFn: () =>
      api(`/experiments/${id}/cancel`, experimentSchema, { method: "POST" }),
    onSuccess: () => {
      setRunning(false);
      void cache.invalidateQueries({ queryKey: ["experiment", id] });
    },
  });
  const item = detail.data;
  if (!item)
    return (
      <>
        <Failure error={detail.error} />
        {detail.isPending && <p role="status">正在读取对照…</p>}
      </>
    );
  const terminal = ["completed", "cancelled"].includes(item.status);
  return (
    <>
      <Link className="text-link" to="/experiments">
        返回策略对比
      </Link>
      <div className="page-title">
        <div>
          <div className="eyebrow">PUBLIC DEVELOPMENT COMPARISON</div>
          <h1>{item.manifest.name}</h1>
          <p>
            {stateName[item.status]} · 已归档 {item.finished} / {item.planned}{" "}
            项
          </p>
        </div>
      </div>
      <div className="notice info">
        以下为原公开报告口径，独立评分另列于页尾。{item.scope}
        。未执行或失败的项目保留在总分母中；进行中数字为暂时结果。
      </div>
      <section className="panel padded">
        <h2>固定设置</h2>
        {item.manifest.schema_version === "2.0" && (
          <p>
            日志解释组最多 2 次调用，不执行代码；单次固定策略 1 次尝试，多次组{" "}
            {item.manifest.multi_attempts}{" "}
            次独立尝试。下列预算为每个修复组的总上限。
          </p>
        )}
        <p>
          {item.configuration.provider} /{" "}
          {item.configuration.model_id || "未配置模型"} · 每项{" "}
          {item.manifest.limits.max_model_calls} 次调用 /{" "}
          {item.manifest.limits.max_tokens.toLocaleString()} Token /{" "}
          {item.manifest.limits.max_wall_seconds} 秒 · 工具循环
          {item.manifest.agent_tools ? "开启" : "关闭"} · 经验库关闭
        </p>
        <p className="small-note">
          源码、运行顺序、配置及预算已登记。关闭页面只暂停安排后续项目，当前任务仍继续；返回后可继续运行。
        </p>
        <Failure error={detail.error ?? advance.error ?? cancel.error} />
        {!terminal && (
          <div className="experiment-controls">
            <button
              className="button primary"
              disabled={running || cancel.isPending}
              onClick={() => setRunning(true)}
            >
              运行 / 继续对照
            </button>
            {running && (
              <button
                className="button secondary"
                onClick={() => setRunning(false)}
              >
                暂停安排后续项目
              </button>
            )}
            <button
              className="button secondary"
              disabled={cancel.isPending}
              onClick={() => {
                setRunning(false);
                cancel.mutate();
              }}
            >
              停止对照
            </button>
          </div>
        )}
        <p>
          <a
            className="text-link"
            href={`/api/v1/experiments/${encodeURIComponent(item.id)}/report`}
          >
            下载可复算报告
          </a>
        </p>
        {item.environment_consistent === false && (
          <p className="notice warning">
            执行环境指纹不一致，请检查逐项记录后再比较。
          </p>
        )}
        <details>
          <summary>查看清单与配置指纹</summary>
          <p className="break-all">清单：{item.manifest_sha256}</p>
          <p className="break-all">配置：{item.configuration_sha256}</p>
          <p className="break-all">报告：{item.report_sha256}</p>
          <pre className="log-excerpt">
            {JSON.stringify(item.manifest, null, 2)}
          </pre>
        </details>
      </section>
      <section className="panel padded">
        <h2>公开检查结果与消耗</h2>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>策略</th>
                <th>通过 / 登记总数</th>
                <th>已复现</th>
                <th>模型调用 / Token</th>
                <th>耗时 / 检查</th>
                <th>费用</th>
              </tr>
            </thead>
            <tbody>
              {item.summary.map((s) => (
                <tr key={s.mode}>
                  <td>
                    {modeName(s.mode)}
                    <small className="muted">
                      {" "}
                      · 已归档 {s.finished}/{s.planned}
                    </small>
                  </td>
                  <td>
                    {s.repair_applicable === false
                      ? `不适用；交付 ${s.diagnoses} 份诊断`
                      : `${s.public_verified} / ${s.planned}`}
                  </td>
                  <td>
                    {s.repair_applicable === false
                      ? "未执行"
                      : `${s.reproduced} / ${s.planned}`}
                  </td>
                  <td>
                    {s.calls} 次 / {s.tokens.toLocaleString()}
                    {s.unknown_usage > 0 && (
                      <p className="small-note">
                        {s.unknown_usage} 项用量不完整
                      </p>
                    )}
                  </td>
                  <td>
                    {s.wall_seconds.toFixed(1)} 秒 / {s.checks} 次
                  </td>
                  <td>{money(s.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {item.summary.map((s) => (
          <p key={s.mode} className="small-note">
            {s.repair_applicable === false ? "日志组诊断准确率未评分。" : ""}
            {modeName(s.mode)}：环境阻塞 {s.blocked}，预算停止{" "}
            {s.budget_exhausted}，失败 / 中断 / 取消 {s.interrupted}。总 Token /
            通过数：
            {s.tokens_per_public_verified === null
              ? "不可计算"
              : s.tokens_per_public_verified.toFixed(0)}
            ；总费用 / 通过数：{money(s.cost_per_public_verified)}。
          </p>
        ))}
      </section>
      <section className="panel padded">
        <h2>逐项执行证据</h2>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>案例 / 次数</th>
                <th>策略</th>
                <th>结果</th>
                <th>Token / 检查</th>
                <th>证据</th>
              </tr>
            </thead>
            <tbody>
              {item.rows.map((row, i) => (
                <tr key={i}>
                  <td>
                    {row.case_id} · 第 {row.repeat} 次
                  </td>
                  <td>{modeName(row.mode)}</td>
                  <td>
                    {row.metrics
                      ? row.metrics.public_verified
                        ? "公开检查通过"
                        : (outcomeName[
                            row.metrics.outcome ?? row.metrics.lifecycle
                          ] ?? "未通过")
                      : row.task_id
                        ? "执行或等待归档"
                        : "未执行"}
                  </td>
                  <td>
                    {row.metrics
                      ? `${row.metrics.tokens.toLocaleString()} / ${row.metrics.checks}`
                      : "—"}
                  </td>
                  <td>
                    {row.task_id ? (
                      <Link className="text-link" to={`/tasks/${row.task_id}`}>
                        查看任务
                      </Link>
                    ) : (
                      "—"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <IndependentScore
        id={item.id}
        complete={item.status === "completed"}
        protocol={item.manifest.schema_version}
      />
    </>
  );
}
