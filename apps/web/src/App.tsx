import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  Link,
  NavLink,
  Route,
  Routes,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  ArrowRight,
  ArrowUpRight,
  Check,
  CheckCheck,
  ChevronLeft,
  ChevronRight,
  Code2,
  Download,
  FileCode2,
  FlaskConical,
  GitBranch,
  Github,
  LayoutDashboard,
  ListChecks,
  LoaderCircle,
  Menu,
  Plus,
  Search,
  ShieldCheck,
  Square,
  Terminal,
  TriangleAlert,
  X,
} from "lucide-react";
import { z } from "zod";
import { api, ApiError, artifactUrl, readArtifact } from "./api";
import { AgentActions } from "./AgentActions";
import { LogViewer } from "./LogViewer";
import { Experiments, ExperimentDetail } from "./Experiments";
import {
  ExperienceLibrary,
  ExperienceDetail,
  TaskExperience,
} from "./Experience";
import {
  caseSchema,
  eventSchema,
  healthSchema,
  modelCheckSchema,
  phaseSchema,
  previewSchema,
  taskListSchema,
  taskSchema,
} from "./contracts";
import type { Case, Evidence, Execution, Task, Validation } from "./contracts";

const phases = phaseSchema.options;
const phaseNames: Record<string, string> = {
  ingest: "读取任务",
  preflight: "环境检查",
  reproduce: "失败复现",
  diagnose: "定位原因",
  refine: "修正补丁",
  validate: "独立验证",
  report: "归档报告",
};
const labels: Record<string, string> = {
  queued: "等待执行",
  running: "执行中",
  completed: "已完成",
  failed: "平台错误",
  interrupted: "执行中断",
  cancelling: "正在取消",
  cancelled: "已取消",
  repaired: "本地验证通过",
  blocked: "环境阻塞",
  not_reproduced: "未复现",
  unresolved: "未解决",
  diagnosed_only: "仅完成诊断",
  budget_exhausted: "预算耗尽",
  passed: "通过",
  not_run: "未执行",
  reproduced: "已复现",
};
const terminal = new Set(["completed", "failed", "interrupted", "cancelled"]);
function taskModeName(task: Task) {
  const arm = task.experiment_binding?.arm;
  return arm === "logs"
    ? "仅日志解释"
    : arm === "multi"
      ? "固定策略 · 独立多次"
      : arm === "standard"
        ? "固定策略 · 单次"
        : task.mode === "standard"
          ? "标准诊断"
          : "自适应诊断";
}
function date(value: number) {
  return new Date(value * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
function elapsed(task: Task, now = Date.now() / 1000) {
  const sec = Math.max(
    0,
    Math.round(
      (task.finished_at ?? now) - (task.started_at ?? task.created_at),
    ),
  );
  return `${Math.floor(sec / 60)}分 ${sec % 60}秒`;
}
function Status({ value, label }: { value: string; label?: string }) {
  return (
    <span
      className={`badge ${["repaired", "passed", "reproduced"].includes(value) ? "success" : ["running", "queued", "cancelling"].includes(value) ? "info" : ["failed", "interrupted"].includes(value) ? "danger" : ["blocked", "unresolved", "budget_exhausted", "not_reproduced"].includes(value) ? "warning" : "neutral"}`}
    >
      <span className="status-dot" />
      {label ?? labels[value] ?? value}
    </span>
  );
}
function ErrorBox({ error }: { error: unknown }) {
  return error ? (
    <div className="notice danger" role="alert">
      <TriangleAlert size={17} />
      <span>
        {error instanceof Error ? error.message : "请求失败，请重试"}
        {error instanceof ApiError && error.retryAt !== undefined && (
          <span className="small-note">
            {" "}
            可重试时间：{new Date(error.retryAt * 1000).toLocaleString("zh-CN")}
          </span>
        )}
      </span>
    </div>
  ) : null;
}
function ModelConnectionCheck() {
  const probe = useMutation({
    mutationFn: () => api("/model/check", modelCheckSchema, { method: "POST" }),
  });
  return (
    <div className="model-probe">
      <button
        type="button"
        className="button secondary"
        disabled={probe.isPending}
        onClick={() => probe.mutate()}
      >
        {probe.isPending ? (
          <LoaderCircle className="spin" size={15} />
        ) : (
          <Activity size={15} />
        )}
        检查模型连接
      </button>
      <small>
        读取模型列表，不发起付费生成。实际诊断时仍可能遇到余额或生成错误。
      </small>
      <ErrorBox error={probe.error} />
      {probe.data && (
        <div className="notice success" role="status">
          <Check size={16} />
          <span>
            {probe.data.model_id} · {probe.data.message}
          </span>
        </div>
      )}
    </div>
  );
}
function Empty({ title, description }: { title: string; description: string }) {
  return (
    <div className="empty">
      <div className="empty-icon">
        <Terminal size={23} />
      </div>
      <h3>{title}</h3>
      <p>{description}</p>
    </div>
  );
}
function Loading() {
  return (
    <div className="loading" role="status">
      <LoaderCircle className="spin" size={22} />
      正在读取任务数据…
    </div>
  );
}
function PageTitle({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="page-title">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action}
    </div>
  );
}

export default function App() {
  const [open, setOpen] = useState(false);
  return (
    <div className="shell">
      <aside className={open ? "sidebar open" : "sidebar"}>
        <Link className="brand" to="/" onClick={() => setOpen(false)}>
          <span className="brand-mark">
            <Activity size={24} />
          </span>
          <span>
            BuildMedic<small>CI DIAGNOSTICS</small>
          </span>
        </Link>
        <div className="nav-label">WORKSPACE</div>
        <nav aria-label="主要导航">
          <NavLink end to="/" onClick={() => setOpen(false)}>
            <LayoutDashboard size={18} />
            工作台
          </NavLink>
          <NavLink to="/tasks" onClick={() => setOpen(false)}>
            <ListChecks size={18} />
            诊断任务
          </NavLink>
          <NavLink to="/experiments" onClick={() => setOpen(false)}>
            <FlaskConical size={18} />
            策略对比
          </NavLink>
          <NavLink to="/experience" onClick={() => setOpen(false)}>
            <FileCode2 size={18} />
            任务经验库
          </NavLink>
        </nav>
        <div className="sidebar-bottom">
          <div className="mini-symbol">
            <ShieldCheck size={19} />
            <span>
              证据驱动的修复<small>每个结论都可以被验证</small>
            </span>
          </div>
          <div className="local-label">
            <span />
            本地开发工作区
          </div>
        </div>
        <button
          className="mobile-close icon-button"
          onClick={() => setOpen(false)}
          aria-label="关闭导航"
        >
          <X />
        </button>
      </aside>
      {open && (
        <button
          className="scrim"
          aria-label="关闭导航"
          onClick={() => setOpen(false)}
        />
      )}
      <div className="main-shell">
        <header className="topbar">
          <div className="topbar-left">
            <button
              className="mobile-menu icon-button"
              onClick={() => setOpen(true)}
              aria-label="打开导航"
            >
              <Menu size={20} />
            </button>
            <span className="workspace-dot" />
            BuildMedic <span className="slash">/</span>
            <span className="muted">开发工作区</span>
          </div>
          <div className="topbar-right">
            <span className="version">v0.2 · CI 诊断</span>
            <a href="/docs" target="_blank" rel="noreferrer">
              API 文档
              <ArrowUpRight size={14} />
            </a>
            <span className="avatar">B</span>
          </div>
        </header>
        <main>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/tasks/new" element={<NewTask />} />
            <Route path="/tasks" element={<TaskList />} />
            <Route path="/tasks/:id" element={<TaskDetail />} />
            <Route path="/experiments" element={<Experiments />} />
            <Route path="/experiments/:id" element={<ExperimentDetail />} />
            <Route path="/experience" element={<ExperienceLibrary />} />
            <Route path="/experience/:id" element={<ExperienceDetail />} />
            <Route
              path="*"
              element={
                <Empty
                  title="页面不存在"
                  description="请从左侧导航返回工作台。"
                />
              }
            />
          </Routes>
        </main>
        <footer>
          BUILDMEDIC <span>固定快照 · 可追溯证据 · 独立验证</span>
        </footer>
      </div>
    </div>
  );
}

function Dashboard() {
  const tasks = useQuery({
    queryKey: ["recent"],
    queryFn: () => api("/tasks?page_size=5", taskListSchema),
    refetchInterval: 5000,
  });
  const stats = useQuery({
    queryKey: ["stats"],
    queryFn: () =>
      api(
        "/stats",
        z.object({
          total: z.number(),
          active: z.number(),
          repaired: z.number(),
          blocked: z.number(),
        }),
      ),
    refetchInterval: 5000,
  });
  const cases = useQuery({
    queryKey: ["cases"],
    queryFn: () => api("/demo-cases", z.array(caseSchema)),
  });
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => api("/health", healthSchema),
    staleTime: 15000,
  });
  return (
    <>
      <PageTitle
        eyebrow="DIAGNOSE. REPAIR. VERIFY."
        title="让失败的构建，有据可解。"
        description="从一段失败日志出发，复现问题、审核补丁，并用真实测试验证结果。"
        action={
          <Link to="/tasks/new" className="button primary">
            <Plus size={17} />
            新建诊断
          </Link>
        }
      />
      <div className="stats-grid">
        {[
          ["诊断任务", stats.data?.total, ListChecks],
          ["正在执行", stats.data?.active, Activity],
          ["本地修复通过", stats.data?.repaired, CheckCheck],
          ["环境阻塞", stats.data?.blocked, ShieldCheck],
        ].map(([label, value, Icon]) => {
          const I = Icon as typeof Activity;
          return (
            <div className="stat-card" key={String(label)}>
              <div>
                <span>{String(label)}</span>
                <I size={18} />
              </div>
              <strong>{typeof value === "number" ? value : "—"}</strong>
              <small>
                {stats.data?.total === 0
                  ? "暂无统计，开始第一个任务"
                  : "根据当前工作区的真实任务统计"}
              </small>
            </div>
          );
        })}
      </div>
      <ErrorBox error={stats.error} />
      {health.data &&
        (!health.data.model_ready || !health.data.runner.ready) && (
          <div className="notice warning">
            <TriangleAlert size={18} />
            <div>
              <strong>运行环境需要配置</strong>
              <p>
                {!health.data.runner.ready
                  ? health.data.runner.message
                  : "Runner 已就绪。"}{" "}
                {!health.data.model_ready &&
                  "尚未配置真实模型；任务会保留已复现的失败并说明阻塞原因。"}
              </p>
            </div>
            <a href="/docs" target="_blank" rel="noreferrer">
              查看接口
              <ArrowUpRight size={14} />
            </a>
          </div>
        )}
      <section className="panel recent-panel">
        <div className="section-header">
          <div>
            <h2>
              近期诊断
              <span className="subtle-count">{stats.data?.total ?? "—"}</span>
            </h2>
            <p>每一次执行，都留下完整证据。</p>
          </div>
          <Link to="/tasks" className="text-link">
            查看全部
            <ArrowRight size={15} />
          </Link>
        </div>
        <ErrorBox error={tasks.error} />
        {tasks.isPending ? (
          <Loading />
        ) : (
          tasks.data && <TaskTable tasks={tasks.data.items} />
        )}
      </section>
      <div className="section-header case-heading">
        <div>
          <div className="eyebrow">START WITH A REAL FAILURE</div>
          <h2>从一个真实案例开始</h2>
          <p>固定源码和 pytest 测试，在登记环境中实际执行。</p>
        </div>
        <span className="outlined-label">
          <Code2 size={14} />
          Python / pytest
        </span>
      </div>
      <ErrorBox error={cases.error} />
      <div className="case-grid">
        {cases.data
          ?.filter(
            (c) => !["missing-dependency", "not-reproduced"].includes(c.id),
          )
          .map((c, i) => (
            <CaseCard key={c.id} item={c} index={i} />
          ))}
      </div>
      <section className="workflow-strip">
        <ShieldCheck size={28} />
        <div>
          <strong>结果可以审核，验证范围清晰</strong>
          <p>本地通过只代表登记环境内的检查通过。远端 CI 结果单独记录。</p>
        </div>
        <div className="workflow-nodes">
          <span>失败复现</span>
          <ArrowRight size={14} />
          <span>源码补丁</span>
          <ArrowRight size={14} />
          <span>独立验证</span>
        </div>
      </section>
    </>
  );
}
function CaseCard({ item, index }: { item: Case; index: number }) {
  return (
    <Link to={`/tasks/new?case=${item.id}`} className="case-card">
      <div className="case-card-top">
        <span className={`case-icon case-${index}`}>
          <span>{["[ ]", "↗", "ƒ(x)"][index]}</span>
        </span>
        <span className="case-number">
          CASE {String(index + 1).padStart(2, "0")}
        </span>
        <ArrowUpRight size={17} />
      </div>
      <span className="case-category">{item.category}</span>
      <h3>{item.title}</h3>
      <p>{item.description}</p>
      <div className="case-footer">
        <span>真实失败测试</span>
        <span>
          开始诊断 <ArrowRight size={14} />
        </span>
      </div>
    </Link>
  );
}
function TaskTable({ tasks }: { tasks: Task[] }) {
  if (!tasks.length)
    return (
      <Empty
        title="还没有诊断任务"
        description="选择下方演示案例，或创建一个 GitHub Actions 日志诊断任务。"
      />
    );
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>任务 / 仓库</th>
            <th>状态与结论</th>
            <th>诊断模式</th>
            <th>创建时间</th>
            <th aria-label="查看" />
          </tr>
        </thead>
        <tbody>
          {tasks.map((t) => (
            <tr key={t.id}>
              <td>
                <Link className="task-name" to={`/tasks/${t.id}`}>
                  <span className="task-icon">
                    <GitBranch size={17} />
                  </span>
                  <span>
                    <strong>{t.title}</strong>
                    <small>
                      {t.repo} · {t.id.slice(0, 8)}
                    </small>
                  </span>
                </Link>
              </td>
              <td>
                <div className="status-stack">
                  <Status value={t.lifecycle} />
                  {t.outcome && (
                    <span className="outcome-text">
                      {labels[t.outcome] ?? t.outcome}
                    </span>
                  )}
                </div>
              </td>
              <td>
                <span className="mode-label">{taskModeName(t)}</span>
              </td>
              <td className="muted nowrap">{date(t.created_at)}</td>
              <td>
                <Link
                  to={`/tasks/${t.id}`}
                  className="icon-button"
                  aria-label={`查看${t.title}`}
                >
                  <ChevronRight size={18} />
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function NewTask() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [source, setSource] = useState<"demo" | "github">("demo");
  const [selected, setSelected] = useState(params.get("case") ?? "pagination");
  const [url, setUrl] = useState("");
  const [jobId, setJobId] = useState<number | null>(null);
  const [seconds, setSeconds] = useState(600);
  const [maxCalls, setMaxCalls] = useState(8);
  const [maxTokens, setMaxTokens] = useState(48000);
  const [mode, setMode] = useState<"standard" | "adaptive">("standard");
  const [experience, setExperience] = useState(false);
  const [agentTools, setAgentTools] = useState(false);
  const key = useRef(crypto.randomUUID());
  const cases = useQuery({
    queryKey: ["cases"],
    queryFn: () => api("/demo-cases", z.array(caseSchema)),
  });
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => api("/health", healthSchema),
  });
  const preview = useMutation({
    mutationFn: () =>
      api("/ci/preview", previewSchema, {
        method: "POST",
        body: JSON.stringify({ run_url: url }),
      }),
    onSuccess: (data) => {
      setJobId(data.jobs.length === 1 ? data.jobs[0].id : null);
      setMode("standard");
      setAgentTools(false);
      setMaxCalls(Math.min(maxCalls, 12));
      setMaxTokens(Math.min(maxTokens, 100000));
      changed();
    },
  });
  const item = cases.data?.find((c) => c.id === selected);
  const selectedJob = preview.data?.jobs.find((job) => job.id === jobId);
  const registeredSource =
    source === "demo" || selectedJob?.execution_supported === true;
  const create = useMutation({
    mutationFn: () =>
      api("/tasks", z.object({ task_id: z.string() }), {
        method: "POST",
        headers: { "Idempotency-Key": key.current },
        body: JSON.stringify({
          source:
            source === "demo"
              ? { type: "demo", case_id: selected }
              : {
                  type: "github_actions",
                  run_url: url,
                  job_id: jobId,
                  run_attempt: preview.data?.run_attempt,
                },
          mode,
          experience,
          agent_tools: agentTools,
          limits: {
            max_wall_seconds: seconds,
            max_model_calls: maxCalls,
            max_tokens: maxTokens,
          },
        }),
      }),
    onSuccess: (data) => navigate(`/tasks/${data.task_id}`),
  });
  function changed() {
    key.current = crypto.randomUUID();
    create.reset();
  }
  function submit(e: FormEvent) {
    e.preventDefault();
    if (!create.isPending) create.mutate();
  }
  return (
    <>
      <Link to="/" className="back-link">
        <ChevronLeft size={16} />
        返回工作台
      </Link>
      <PageTitle
        eyebrow="NEW DIAGNOSIS"
        title="创建一次诊断"
        description="选择失败来源，固定任务版本，然后交给 BuildMedic。"
      />
      <div className="create-grid">
        <form className="panel form-panel" onSubmit={submit}>
          <div className="form-section">
            <div className="step-heading">
              <span>01</span>
              <h2>选择失败来源</h2>
            </div>
            <div className="segmented">
              <button
                type="button"
                className={source === "demo" ? "active" : ""}
                onClick={() => {
                  setSource("demo");
                  changed();
                }}
              >
                <FlaskConical size={16} />
                演示案例
              </button>
              <button
                type="button"
                className={source === "github" ? "active" : ""}
                onClick={() => {
                  setSource("github");
                  setExperience(false);
                  setAgentTools(false);
                  setMaxCalls(Math.min(maxCalls, 12));
                  setMaxTokens(Math.min(maxTokens, 100000));
                  setMode("standard");
                  changed();
                }}
              >
                <Github size={16} />
                GitHub Actions
              </button>
            </div>
            {source === "demo" ? (
              <>
                <ErrorBox error={cases.error} />
                <div className="case-options">
                  {cases.data?.map((c) => (
                    <label
                      className={`case-option ${selected === c.id ? "selected" : ""}`}
                      key={c.id}
                    >
                      <input
                        type="radio"
                        name="case"
                        value={c.id}
                        checked={selected === c.id}
                        onChange={() => {
                          setSelected(c.id);
                          changed();
                        }}
                      />
                      <div>
                        <strong>{c.title}</strong>
                        <p>{c.description}</p>
                        <small>
                          {c.category} · pytest{" "}
                          {c.blocked ? "· 预期环境阻塞" : ""}
                        </small>
                      </div>
                      {selected === c.id && <Check size={17} />}
                    </label>
                  ))}
                </div>
              </>
            ) : (
              <div className="github-form">
                <label htmlFor="run-url">Actions Run URL</label>
                <input
                  id="run-url"
                  type="url"
                  value={url}
                  required
                  placeholder="https://github.com/owner/repo/actions/runs/123"
                  onChange={(e) => {
                    setUrl(e.target.value);
                    preview.reset();
                    setJobId(null);
                    setMode("standard");
                    setAgentTools(false);
                    setMaxCalls(Math.min(maxCalls, 12));
                    setMaxTokens(Math.min(maxTokens, 100000));
                    changed();
                  }}
                />
                <button
                  type="button"
                  className="button secondary"
                  disabled={!url || preview.isPending}
                  onClick={() => preview.mutate()}
                >
                  {preview.isPending ? (
                    <LoaderCircle className="spin" size={16} />
                  ) : (
                    <Search size={16} />
                  )}
                  读取运行记录
                </button>
                <ErrorBox error={preview.error} />
                {preview.data && (
                  <div className="preview-box">
                    <div className="mono">
                      {preview.data.repo}
                      <br />
                      {preview.data.head_sha}
                      <br />
                      Run #{preview.data.run_id} / attempt{" "}
                      {preview.data.run_attempt}
                    </div>
                    <div className="notice warning">{preview.data.message}</div>
                    {preview.data.jobs.length ? (
                      preview.data.jobs.map((j) => (
                        <label className="job-option" key={j.id}>
                          <input
                            type="radio"
                            name="job"
                            checked={jobId === j.id}
                            onChange={() => {
                              setJobId(j.id);
                              if (!j.execution_supported) {
                                setMode("standard");
                                setAgentTools(false);
                                setMaxCalls(Math.min(maxCalls, 12));
                                setMaxTokens(Math.min(maxTokens, 100000));
                              }
                              changed();
                            }}
                          />
                          <span>
                            <strong>{j.name}</strong>
                            <small>
                              {j.execution_supported
                                ? `可独立复现 · ${j.profile_version}`
                                : "仅日志诊断 · 尚未登记"}
                            </small>
                            <small>
                              {j.failed_steps
                                .map((s) => `Step ${s.number}: ${s.name}`)
                                .join(" / ") || j.conclusion}
                            </small>
                          </span>
                        </label>
                      ))
                    ) : (
                      <p>指定运行没有可诊断的失败 Job。</p>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>
          <div className="form-section">
            <div className="step-heading">
              <span>02</span>
              <h2>诊断模式与预算</h2>
            </div>
            <div className="mode-cards" role="radiogroup" aria-label="诊断模式">
              <button
                type="button"
                role="radio"
                aria-checked={mode === "standard"}
                className={`mode-card ${mode === "standard" ? "selected" : ""}`}
                onClick={() => {
                  setMode("standard");
                  setExperience(false);
                  setMaxCalls(agentTools ? 16 : Math.min(maxCalls, 12));
                  setMaxTokens(
                    agentTools ? 150000 : Math.min(maxTokens, 100000),
                  );
                  changed();
                }}
              >
                <div>
                  <strong>
                    <Activity size={16} />
                    标准诊断
                  </strong>
                  {mode === "standard" && <Check size={17} />}
                </div>
                <p>固定策略诊断，最多两次补丁尝试，以独立回归结果验收。</p>
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={mode === "adaptive"}
                disabled={!health.data?.adaptive_ready || !registeredSource}
                className={`mode-card ${mode === "adaptive" ? "selected" : ""}`}
                onClick={() => {
                  setMode("adaptive");
                  setMaxCalls(agentTools ? 24 : 12);
                  setMaxTokens(agentTools ? 250000 : 100000);
                  changed();
                }}
              >
                <div>
                  <strong>
                    <FlaskConical size={16} />
                    自适应诊断
                  </strong>
                  {mode === "adaptive" ? (
                    <Check size={17} />
                  ) : (
                    <span className="small-tag">
                      {health.data?.adaptive_ready && registeredSource
                        ? "Harness²"
                        : "未就绪"}
                    </span>
                  )}
                </div>
                <p>
                  比较并组合诊断策略：4 次独立执行、3
                  次策略提议，选中补丁仍需独立验收。
                </p>
              </button>
            </div>
            {mode === "adaptive" && source === "demo" && (
              <label className="experience-option">
                <input
                  type="checkbox"
                  checked={experience}
                  onChange={(e) => {
                    setExperience(e.target.checked);
                    if (e.target.checked) {
                      setMaxCalls(agentTools ? 24 : 12);
                      setMaxTokens(agentTools ? 250000 : 150000);
                    } else {
                      setMaxTokens(
                        Math.min(maxTokens, agentTools ? 250000 : 100000),
                      );
                    }
                    changed();
                  }}
                />
                <span>
                  <strong>使用同域经验并整理任务反思</strong>
                  <small>
                    提交时固定当前会话的最新兼容经验，至少 8 次调用。默认
                    150,000 Token
                    预算，可手动降低；最终验证通过后可手动发布。尚未评测跨任务效果。
                  </small>
                </span>
              </label>
            )}
            {mode === "adaptive" && (
              <div className="notice info">
                已启用 Harness² k=1
                顺序优化，全部调用共享预算。支持登记的案例和固定提交
                Job；不会自动发布跨任务经验。
              </div>
            )}
            {!health.data?.adaptive_ready && (
              <p className="small-note">{health.data?.adaptive_message}</p>
            )}
            <label className="experience-option">
              <input
                type="checkbox"
                checked={agentTools}
                disabled={!registeredSource || !health.data?.agent_tools_ready}
                onChange={(e) => {
                  const enabled = e.target.checked;
                  setAgentTools(enabled);
                  setMaxCalls(
                    enabled
                      ? mode === "adaptive"
                        ? 24
                        : 16
                      : mode === "adaptive"
                        ? 12
                        : 8,
                  );
                  setMaxTokens(
                    enabled
                      ? mode === "adaptive"
                        ? 250000
                        : 150000
                      : experience
                        ? 150000
                        : mode === "adaptive"
                          ? 100000
                          : 48000,
                  );
                  changed();
                }}
              />
              <span>
                <strong>启用 Agent 工具诊断</strong>
                <small>
                  按需读日志、检索源码、提交候选并请求登记检查。每轮最多 6
                  次模型调用、2
                  次工具测试，共享任务预算；需兼容工具调用的模型和已登记来源。
                </small>
              </span>
            </label>
            <div className="budget-fields">
              <label>
                最长运行时间
                <select
                  value={seconds}
                  onChange={(e) => {
                    setSeconds(Number(e.target.value));
                    changed();
                  }}
                >
                  <option value={300}>5 分钟</option>
                  <option value={600}>10 分钟</option>
                  <option value={1200}>20 分钟</option>
                </select>
              </label>
              <label>
                最多模型调用
                <select
                  value={maxCalls}
                  onChange={(e) => {
                    setMaxCalls(Number(e.target.value));
                    changed();
                  }}
                >
                  <option value={2} disabled={mode === "adaptive"}>
                    2 次
                  </option>
                  <option value={4} disabled={mode === "adaptive"}>
                    4 次
                  </option>
                  <option value={8}>8 次</option>
                  <option value={12}>12 次</option>
                  {agentTools &&
                    [16, 24, 32].map((value) => (
                      <option
                        key={value}
                        value={value}
                        disabled={value > (health.data?.max_model_calls ?? 12)}
                      >
                        {value} 次（含工具交互）
                      </option>
                    ))}
                </select>
              </label>
              <label>
                Token 上限
                <select
                  value={maxTokens}
                  onChange={(e) => {
                    setMaxTokens(Number(e.target.value));
                    changed();
                  }}
                >
                  <option value={24000} disabled={mode === "adaptive"}>
                    24,000
                  </option>
                  <option value={48000}>48,000</option>
                  <option value={100000}>100,000</option>
                  {(experience || agentTools) && (
                    <option value={150000}>
                      150,000（{agentTools ? "含工具交互" : "含经验反思"}）
                    </option>
                  )}
                  {agentTools && (
                    <option value={250000}>250,000（含工具交互）</option>
                  )}
                </select>
              </label>
            </div>
          </div>
          <div className="form-submit">
            <ErrorBox error={create.error} />
            <button
              className="button primary"
              disabled={
                create.isPending ||
                (source === "demo" ? !item : !preview.data || !jobId)
              }
            >
              {create.isPending ? (
                <LoaderCircle className="spin" size={17} />
              ) : (
                <Activity size={17} />
              )}
              开始诊断
              <ArrowRight size={16} />
            </button>
            <small>提交后可查看实时执行进度。</small>
          </div>
        </form>
        <aside className="create-aside">
          <div className="panel summary-panel">
            <div className="eyebrow">TASK SNAPSHOT</div>
            <h3>运行前确认</h3>
            <dl>
              <dt>任务来源</dt>
              <dd>{source === "demo" ? "本地真实案例" : "GitHub Actions"}</dd>
              <dt>仓库</dt>
              <dd>
                {source === "demo"
                  ? item?.repo
                  : (preview.data?.repo ?? "尚未读取")}
              </dd>
              <dt>{source === "demo" ? "源码快照 SHA256" : "提交 SHA"}</dt>
              <dd className="mono break-all">
                {source === "demo"
                  ? (item?.snapshot_sha256 ?? "环境阻塞")
                  : (preview.data?.head_sha ?? "—")}
              </dd>
              <dt>复现 Profile</dt>
              <dd className="mono">
                {source === "demo"
                  ? item?.profile_version
                  : (selectedJob?.profile_version ?? "尚未登记")}
              </dd>
            </dl>
            <div className="small-note">
              <ShieldCheck size={17} />
              <p>模型密钥与 GitHub 凭据仅在服务端配置。</p>
            </div>
          </div>
          {health.data && (
            <div className="panel environment-panel">
              <h3>服务就绪状态</h3>
              <p>
                <span
                  className={`health-dot ${health.data.runner.ready ? "ready" : ""}`}
                />
                Runner {health.data.runner.ready ? "已就绪" : "未就绪"}
              </p>
              <p>
                <span
                  className={`health-dot ${health.data.model_ready ? "ready" : ""}`}
                />
                模型 {health.data.model_ready ? "已配置" : "未配置"}
              </p>
              <small>{health.data.runner.message}</small>
              {health.data.model_ready && (
                <>
                  <p className="mono break-all">{health.data.model_id}</p>
                  <ModelConnectionCheck />
                </>
              )}
            </div>
          )}
        </aside>
      </div>
    </>
  );
}

function TaskList() {
  const [status, setStatus] = useState("");
  const [repo, setRepo] = useState("");
  const [page, setPage] = useState(1);
  const tasks = useQuery({
    queryKey: ["tasks", status, repo, page],
    queryFn: () =>
      api(
        `/tasks?page=${page}&page_size=10${status ? `&lifecycle=${status}` : ""}${repo ? `&repo=${encodeURIComponent(repo)}` : ""}`,
        taskListSchema,
      ),
    refetchInterval: 5000,
  });
  return (
    <>
      <PageTitle
        eyebrow="DIAGNOSIS HISTORY"
        title="诊断任务"
        description="查看当前工作区的执行记录、证据和独立验证结果。"
        action={
          <Link to="/tasks/new" className="button primary">
            <Plus size={17} />
            新建诊断
          </Link>
        }
      />
      <section className="panel">
        <div className="table-toolbar">
          <label className="search-field">
            <Search size={17} />
            <input
              aria-label="按仓库筛选"
              value={repo}
              placeholder="搜索仓库…"
              onChange={(e) => {
                setRepo(e.target.value);
                setPage(1);
              }}
            />
          </label>
          <select
            aria-label="按状态筛选"
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              setPage(1);
            }}
          >
            <option value="">全部状态</option>
            {[
              "queued",
              "running",
              "completed",
              "blocked",
              "failed",
              "interrupted",
              "cancelled",
            ]
              .filter((s) => s !== "blocked")
              .map((s) => (
                <option key={s} value={s}>
                  {labels[s]}
                </option>
              ))}
          </select>
          <span className="muted">{tasks.data?.total ?? "—"} 个任务</span>
        </div>
        <ErrorBox error={tasks.error} />
        {tasks.isPending ? (
          <Loading />
        ) : (
          tasks.data && <TaskTable tasks={tasks.data.items} />
        )}
        <div className="pagination">
          <span>第 {page} 页</span>
          <button
            className="icon-button"
            aria-label="上一页"
            disabled={page === 1}
            onClick={() => setPage((p) => p - 1)}
          >
            <ChevronLeft size={17} />
          </button>
          <button
            className="icon-button"
            aria-label="下一页"
            disabled={!tasks.data || page * 10 >= tasks.data.total}
            onClick={() => setPage((p) => p + 1)}
          >
            <ChevronRight size={17} />
          </button>
        </div>
      </section>
    </>
  );
}

function useTask(id: string) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["task", id],
    queryFn: () => api(`/tasks/${id}`, taskSchema),
    refetchInterval: (q) =>
      q.state.data &&
      terminal.has(q.state.data.lifecycle) &&
      !q.state.data.validations.some((v) =>
        ["queued", "running"].includes(v.status),
      )
        ? false
        : 1000,
  });
  const [connection, setConnection] = useState("正在连接");
  const finished = query.data ? terminal.has(query.data.lifecycle) : false;
  const loaded = !!query.data;
  useEffect(() => {
    if (!loaded || finished) return;
    let cursor = query.data?.last_event_seq ?? 0;
    const source = new EventSource(
      `/api/v1/tasks/${encodeURIComponent(id)}/events?after=${cursor}`,
    );
    const names = [
      "task.queued",
      "task.started",
      "phase.changed",
      "evidence.created",
      "execution.started",
      "execution.finished",
      "environment.checked",
      "reproduction.finished",
      "diagnosis.created",
      "rollout.started",
      "rollout.finished",
      "patch.created",
      "candidate.selected",
      "validation.started",
      "validation.queued",
      "validation.finished",
      "artifact.created",
      "usage.updated",
      "model.started",
      "model.finished",
      "harness.started",
      "harness.proposed",
      "harness.interrupted",
      "harness.finished",
      "experience.reflecting",
      "experience.reflected",
      "experience.rejected",
      "experience.available",
      "experience.unverified",
      "experience.published",
      "snapshot.prepared",
      "agent.tool_started",
      "agent.tool_finished",
      "worker.lost",
      "task.cancelling",
      "task.finished",
    ];
    let resetTimer: ReturnType<typeof setTimeout> | undefined;
    const refresh = () =>
      void queryClient.invalidateQueries({ queryKey: ["task", id] });
    for (const name of names)
      source.addEventListener(name, (raw) => {
        try {
          const parsed = eventSchema.parse(
            JSON.parse((raw as MessageEvent).data),
          );
          if (parsed.task_id !== id || parsed.seq <= cursor) return;
          cursor = parsed.seq;
          refresh();
          if (name === "task.finished") source.close();
        } catch {
          setConnection("数据重新同步中");
          source.close();
          refresh();
          resetTimer = setTimeout(() => window.location.reload(), 1000);
        }
      });
    source.onopen = () => setConnection("实时连接");
    source.onerror = () => setConnection("连接恢复中");
    source.addEventListener("stream.reset", () => {
      source.close();
      refresh();
      setConnection("状态重新同步中");
      resetTimer = setTimeout(() => window.location.reload(), 500);
    });
    return () => {
      source.close();
      if (resetTimer) clearTimeout(resetTimer);
    };
  }, [id, loaded, finished, queryClient]);
  return { ...query, connection };
}

function TaskDetail() {
  const { id = "" } = useParams();
  const [params] = useSearchParams();
  const requestedTab = [
    "overview",
    "evidence",
    "patch",
    "validation",
    "strategy",
  ].includes(params.get("tab") ?? "")
    ? params.get("tab")!
    : "overview";
  const query = useTask(id);
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const retryKey = useRef(crypto.randomUUID());
  const [tab, setTab] = useState(requestedTab);
  const [focus, setFocus] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now() / 1000);
  const refresh = () =>
    void queryClient.invalidateQueries({ queryKey: ["task", id] });
  const cancel = useMutation({
    mutationFn: () =>
      api(`/tasks/${id}/cancel`, taskSchema, { method: "POST" }),
    onSuccess: refresh,
  });
  const retry = useMutation({
    mutationFn: () =>
      api(`/tasks/${id}/retry`, z.object({ task_id: z.string() }), {
        method: "POST",
        headers: { "Idempotency-Key": retryKey.current },
      }),
    onSuccess: (data) => navigate(`/tasks/${data.task_id}`),
  });
  useEffect(() => {
    retryKey.current = crypto.randomUUID();
    setTab(requestedTab);
    setFocus(null);
  }, [id, requestedTab]);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, []);
  if (query.isPending) return <Loading />;
  if (query.error && !query.data) return <ErrorBox error={query.error} />;
  const task = query.data;
  if (!task) return null;
  const done = terminal.has(task.lifecycle);
  const report = [...task.artifacts].reverse().find((a) => a.kind === "report");
  const evidenceLink = (ev: string) => {
    setFocus(ev);
    setTab("evidence");
  };
  return (
    <>
      <Link to="/tasks" className="back-link">
        <ChevronLeft size={16} />
        全部诊断任务
      </Link>
      <div className="task-detail-heading">
        <div>
          <div className="eyebrow">
            DIAGNOSIS <span className="mono">#{task.id.slice(0, 8)}</span>
          </div>
          <h1>{task.title}</h1>
          <p>
            <GitBranch size={15} />
            {task.repo}
            <span>·</span>
            {task.execution_label}
          </p>
        </div>
        <div className="task-actions">
          {done ? (
            <button
              className="button secondary"
              disabled={
                retry.isPending ||
                task.experiment_binding?.protocol === "four-arm-v1"
              }
              onClick={() => retry.mutate()}
            >
              <Plus size={16} />
              重新诊断
            </button>
          ) : (
            <button
              className="button secondary"
              disabled={cancel.isPending || task.lifecycle === "cancelling"}
              onClick={() => cancel.mutate()}
            >
              <Square size={13} />
              取消任务
            </button>
          )}
          {report && (
            <a
              className="button primary"
              href={artifactUrl(id, report.id, true)}
            >
              <Download size={16} />
              下载报告
            </a>
          )}
        </div>
      </div>
      <ErrorBox error={cancel.error ?? retry.error} />
      <div className="task-meta-strip">
        <div>
          <small>执行状态</small>
          <Status value={task.lifecycle} />
        </div>
        <div>
          <small>业务结论</small>
          {task.outcome ? (
            <Status value={task.outcome} />
          ) : (
            <span>等待执行结论</span>
          )}
        </div>
        <div>
          <small>已耗时</small>
          <strong className="mono">{elapsed(task, now)}</strong>
        </div>
        <div>
          <small>诊断模式</small>
          <strong>{taskModeName(task)}</strong>
        </div>
        <div>
          <small>状态同步</small>
          <span className="connection">
            <span
              className={`health-dot ${done || query.connection === "实时连接" ? "ready" : ""}`}
            />
            {done ? "历史结果已保存" : query.connection}
          </span>
        </div>
      </div>
      <div className="detail-grid">
        <section className="panel detail-main">
          <div className="detail-tabs" role="tablist" aria-label="诊断详情">
            {[
              ["overview", "概览"],
              ["evidence", "证据与日志"],
              ["patch", "修复方案"],
              ["validation", "验证结果"],
              ["strategy", "策略记录"],
            ].map(([value, label]) => (
              <button
                role="tab"
                aria-selected={tab === value}
                key={value}
                className={tab === value ? "active" : ""}
                onClick={() => setTab(value)}
              >
                {label}
                {value === "patch" && task.candidates.length > 0 && (
                  <span>{task.candidates.length}</span>
                )}
              </button>
            ))}
          </div>
          <div className="tab-body" role="tabpanel">
            {tab === "overview" && (
              <Overview task={task} evidenceLink={evidenceLink} />
            )}{" "}
            {tab === "evidence" && <EvidencePanel task={task} focus={focus} />}{" "}
            {tab === "patch" && <PatchPanel task={task} refresh={refresh} />}{" "}
            {tab === "validation" && <ValidationPanel task={task} />}{" "}
            {tab === "strategy" && <StrategyPanel task={task} />}
          </div>
        </section>
        <aside className="detail-aside">
          <div className="panel phase-panel">
            <h3>执行阶段</h3>
            <div className="phase-list">
              {phases.map((p, i) => {
                const current = phases.indexOf(task.phase);
                const visited = (task.phase_history ?? [task.phase]).includes(
                  p,
                );
                const complete = visited && (i !== current || done);
                return (
                  <div
                    key={p}
                    className={`phase-step ${complete ? "complete" : i === current ? "current" : ""}`}
                  >
                    <span className="phase-node">
                      {complete ? (
                        <Check size={12} />
                      ) : i === current && !done ? (
                        <LoaderCircle className="spin" size={12} />
                      ) : (
                        i + 1
                      )}
                    </span>
                    <div>
                      <strong>
                        {task.mode === "adaptive" && p === "refine"
                          ? "优化诊断策略"
                          : phaseNames[p]}
                      </strong>
                      <small>
                        {complete
                          ? "已执行"
                          : i === current
                            ? done
                              ? "已结束"
                              : "进行中"
                            : done
                              ? "未执行"
                              : "等待"}
                      </small>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
          <div className="panel summary-panel">
            <h3>执行摘要</h3>
            <dl>
              <dt>固定版本</dt>
              <dd className="mono break-all">
                {task.head_sha ?? task.snapshot_sha256 ?? "无可执行快照"}
              </dd>
              <dt>Run / attempt / Job</dt>
              <dd>
                {task.run_id
                  ? `${task.run_id} / ${task.run_attempt} / ${task.job_id}`
                  : "本地案例，无远端运行记录"}
              </dd>
              <dt>复现 Profile</dt>
              <dd className="mono">{task.profile_version ?? "尚未登记"}</dd>
              {task.source_snapshot && (
                <>
                  <dt>远程源码清单 SHA256</dt>
                  <dd className="mono break-all">
                    {task.source_snapshot.snapshot_sha256}
                  </dd>
                  <dt>Profile 指纹</dt>
                  <dd className="mono break-all">
                    {task.source_snapshot.profile_hash}
                  </dd>
                  <dt>固定快照归档</dt>
                  <dd>
                    <a
                      className="text-link"
                      href={artifactUrl(
                        task.id,
                        task.source_snapshot.manifest_artifact_id,
                        true,
                      )}
                    >
                      下载完整性清单
                    </a>
                    {task.source_snapshot.profile_artifact_id && (
                      <>
                        <br />
                        <a
                          className="text-link"
                          href={artifactUrl(
                            task.id,
                            task.source_snapshot.profile_artifact_id,
                            true,
                          )}
                        >
                          下载登记环境
                        </a>
                      </>
                    )}
                  </dd>
                </>
              )}
              <dt>请求模型</dt>
              <dd className="mono break-all">
                {task.usage.model ?? "尚未调用"}
              </dd>
              {task.usage.response_model && (
                <>
                  <dt>实际响应模型</dt>
                  <dd className="mono break-all">
                    {task.usage.response_model}
                  </dd>
                </>
              )}
              <dt>模型调用</dt>
              <dd>
                {task.usage.calls} 次 / 最多 {task.limits.max_model_calls} 次
              </dd>
              <dt>Token 用量</dt>
              <dd>
                {task.usage.usage_known
                  ? task.usage.tokens.toLocaleString()
                  : "未知（已按保守额度计入预算）"}
              </dd>
              <dt>费用</dt>
              <dd>
                {task.usage.cost_usd === null
                  ? "未配置计价，费用未知"
                  : `$${task.usage.cost_usd.toFixed(4)}`}
              </dd>
            </dl>
            <div className="small-note">
              <ShieldCheck size={16} />
              <p>验证范围：登记环境内的本地检查。</p>
            </div>
          </div>
        </aside>
      </div>
    </>
  );
}

function Overview({
  task,
  evidenceLink,
}: {
  task: Task;
  evidenceLink: (id: string) => void;
}) {
  return (
    <>
      <div
        className={`result-banner ${task.outcome === "repaired" ? "success" : task.outcome ? "warning" : ""}`}
      >
        <div className="result-icon">
          {task.outcome === "repaired" ? (
            <ShieldCheck size={24} />
          ) : terminal.has(task.lifecycle) ? (
            <TriangleAlert size={23} />
          ) : (
            <Activity size={24} />
          )}
        </div>
        <div>
          <span className="eyebrow">
            {terminal.has(task.lifecycle)
              ? "DIAGNOSIS RESULT"
              : "LIVE DIAGNOSIS"}
          </span>
          <h2>
            {task.outcome
              ? (labels[task.outcome] ?? task.outcome)
              : task.mode === "adaptive" && task.phase === "refine"
                ? "优化诊断策略"
                : phaseNames[task.phase]}
          </h2>
          <p>{task.message}</p>
        </div>
      </div>
      {task.diagnosis ? (
        <>
          <div className="content-section">
            <h3>根因摘要</h3>
            <p>{task.diagnosis.summary}</p>
            <div className="root-cause">
              <span className="small-tag">
                {task.diagnosis.root_cause.certainty === "supported"
                  ? "证据支持"
                  : task.diagnosis.root_cause.certainty === "hypothesis"
                    ? "待验证假设"
                    : "证据不足"}
              </span>
              <p>{task.diagnosis.root_cause.description}</p>
              <div className="evidence-chips">
                {task.diagnosis.root_cause.evidence_ids.map((ev) => (
                  <button key={ev} onClick={() => evidenceLink(ev)}>
                    <FileCode2 size={13} />
                    {ev}
                  </button>
                ))}
              </div>
            </div>
          </div>
          <div className="content-section">
            <h3>下一步建议</h3>
            <p>{task.diagnosis.next_action}</p>
            {task.diagnosis.limitations.length > 0 && (
              <ul>
                {task.diagnosis.limitations.map((l, i) => (
                  <li key={i}>{l}</li>
                ))}
              </ul>
            )}
          </div>
        </>
      ) : (
        <div className="content-section">
          <h3>诊断证据</h3>
          {task.evidence.length ? (
            <>
              <p>已收集 {task.evidence.length} 条平台证据，点击查看来源。</p>
              <div className="evidence-chips">
                {task.evidence.map((e) => (
                  <button key={e.id} onClick={() => evidenceLink(e.id)}>
                    <FileCode2 size={13} />
                    {e.path ?? e.kind}
                  </button>
                ))}
              </div>
            </>
          ) : (
            <p>执行到相应阶段后，真实日志、代码位置和测试结果会保存在这里。</p>
          )}
        </div>
      )}
      <div className="verification-strip">
        <div>
          <span>修复前</span>
          <Status value={task.reproduction_status} />
        </div>
        <ArrowRight size={18} />
        <div>
          <span>独立验证</span>
          <Status value={task.validation_status} />
        </div>
      </div>
    </>
  );
}
function EvidencePanel({ task, focus }: { task: Task; focus: string | null }) {
  const [search, setSearch] = useState("");
  useEffect(() => {
    if (focus)
      document
        .getElementById(focus)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [focus]);
  return (
    <>
      <label className="search-field evidence-search">
        <Search size={16} />
        <input
          placeholder="搜索证据、文件或日志内容…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="搜索证据"
        />
      </label>
      {task.evidence.length ? (
        task.evidence
          .filter((e) =>
            `${e.path} ${e.excerpt} ${e.id}`
              .toLowerCase()
              .includes(search.toLowerCase()),
          )
          .map((e) => (
            <EvidenceCard
              key={e.id}
              evidence={e}
              taskId={task.id}
              highlighted={focus === e.id}
            />
          ))
      ) : (
        <Empty
          title="暂无证据"
          description="Runner 完成执行后，日志和代码引用将显示在这里。"
        />
      )}
      {task.executions.length > 0 && (
        <div className="content-section">
          <h3>执行日志</h3>
          {task.executions.map((e) => (
            <ExecutionLog key={e.id} execution={e} taskId={task.id} />
          ))}
        </div>
      )}
    </>
  );
}
function EvidenceCard({
  evidence: e,
  taskId,
  highlighted,
}: {
  evidence: Evidence;
  taskId: string;
  highlighted: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const isLog = ["ci_log", "baseline_log", "test_log"].includes(e.kind);
  const text = useQuery({
    queryKey: ["artifact", taskId, e.artifact_id],
    queryFn: () => readArtifact(taskId, e.artifact_id),
    enabled: expanded && !isLog,
  });
  return (
    <article
      id={e.id}
      className={`evidence-card ${highlighted ? "highlighted" : ""}`}
    >
      <div className="evidence-header">
        <span>
          <FileCode2 size={16} />
          <strong>{e.path ?? e.kind}</strong>
        </span>
        <span className="mono">{e.id}</span>
      </div>
      <small>
        来源行号 L{e.line_start}–L{e.line_end}
      </small>
      {e.log_origin && (
        <p className="small-note">
          日志来源：
          {e.log_origin.source === "cache"
            ? "复用本会话缓存"
            : "本次从 GitHub 下载"}
          {e.log_origin.cached_at != null && (
            <>
              ；缓存于{" "}
              {new Date(e.log_origin.cached_at * 1000).toLocaleString("zh-CN")}
            </>
          )}
          {e.log_origin.artifact_id && (
            <>
              {" "}
              ·{" "}
              <a
                className="text-link"
                href={artifactUrl(taskId, e.log_origin.artifact_id, true)}
              >
                下载来源记录
              </a>
            </>
          )}
        </p>
      )}
      <pre className="log-excerpt">{e.excerpt}</pre>
      {e.log_context_artifact_ids?.map((aid, i) => (
        <p key={aid}>
          <a className="text-link" href={artifactUrl(taskId, aid, true)}>
            下载模型使用的日志片段 {i + 1}
          </a>
        </p>
      ))}
      {e.excerpt_truncated && (
        <p className="small-note">摘要已截断，展开上下文可按页查看完整内容。</p>
      )}
      <button className="text-link" onClick={() => setExpanded((v) => !v)}>
        {expanded ? "收起上下文" : "展开完整上下文"}
        <ChevronRight size={14} />
      </button>
      {expanded && isLog && (
        <LogViewer
          key={e.artifact_id}
          taskId={taskId}
          artifactId={e.artifact_id}
          index={e.log_index}
          initialLine={e.line_start}
          highlightEnd={e.line_end}
        />
      )}
      {expanded && !isLog && (
        <>
          <ErrorBox error={text.error} />
          {text.isPending ? (
            <Loading />
          ) : (
            <div className="numbered-log">
              {text.data?.split("\n").map((line, i) => (
                <div
                  className={
                    i + 1 >= e.line_start && i + 1 <= e.line_end
                      ? "highlight"
                      : ""
                  }
                  key={i}
                >
                  <span>{i + 1}</span>
                  <code>{line || " "}</code>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </article>
  );
}
function ExecutionLog({
  execution: e,
  taskId,
}: {
  execution: Execution;
  taskId: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="execution-log">
      <button onClick={() => setOpen((v) => !v)}>
        <Terminal size={15} />
        <span>
          {e.check_id === "target" ? "目标测试" : "完整回归"} ·{" "}
          {e.id.slice(0, 8)}
        </span>
        <span>exit {e.exit_code ?? "—"}</span>
        <ChevronRight size={15} />
      </button>
      {open && (
        <>
          <code className="command">{e.command?.join(" ") ?? "执行中"}</code>
          {e.stdout_artifact_id ? (
            <LogViewer
              key={e.stdout_artifact_id}
              taskId={taskId}
              artifactId={e.stdout_artifact_id}
            />
          ) : (
            <p>等待日志产物…</p>
          )}
        </>
      )}
    </div>
  );
}
function PatchPanel({ task, refresh }: { task: Task; refresh: () => void }) {
  const [selected, setSelected] = useState("");
  const candidate =
    task.candidates.find((c) => c.id === selected) ??
    task.candidates.find((c) => c.selected) ??
    task.candidates[0];
  const [file, setFile] = useState("");
  const patch = useQuery({
    queryKey: ["artifact", task.id, candidate?.patch_artifact_id],
    queryFn: () => readArtifact(task.id, candidate!.patch_artifact_id),
    enabled: !!candidate,
  });
  const revalidate = useMutation({
    mutationFn: () =>
      api(
        `/tasks/${task.id}/validations`,
        z.object({ validation_id: z.string() }),
        {
          method: "POST",
          body: JSON.stringify({ candidate_id: candidate?.id }),
        },
      ),
    onSuccess: refresh,
  });
  if (!candidate)
    return (
      <Empty
        title="尚无可审阅补丁"
        description="只有模型实际交付并通过范围校验的源码补丁才会显示在这里。"
      />
    );
  const selectedFile = candidate.files.includes(file)
    ? file
    : candidate.files[0];
  const blocks = (patch.data ?? "").split(/(?=^--- a\/)/m);
  const filtered = blocks
    .filter((b) => b.startsWith(`--- a/${selectedFile}\n`))
    .join("");
  const lines = filtered || patch.data || "";
  const active = task.validations.some(
    (v) =>
      v.kind === "revalidation" && ["queued", "running"].includes(v.status),
  );
  return (
    <>
      <div className="patch-toolbar">
        <label>
          候选补丁
          <select
            value={candidate.id}
            onChange={(e) => {
              setSelected(e.target.value);
              setFile("");
            }}
          >
            {task.candidates.map((c, i) => (
              <option value={c.id} key={c.id}>
                候选 {i + 1}
                {c.selected ? " · 已选中" : ""}
              </option>
            ))}
          </select>
        </label>
        <a
          href={artifactUrl(task.id, candidate.patch_artifact_id, true)}
          className="button secondary"
        >
          <Download size={15} />
          下载 patch
        </a>
      </div>
      <p className="patch-explanation">{candidate.explanation}</p>
      <div className="file-tabs">
        {candidate.files.map((f) => (
          <button
            className={f === selectedFile ? "active" : ""}
            key={f}
            onClick={() => setFile(f)}
          >
            <FileCode2 size={14} />
            {f}
          </button>
        ))}
      </div>
      <ErrorBox error={patch.error} />
      {patch.isPending ? (
        <Loading />
      ) : (
        <details className="diff-container" open>
          <summary>{selectedFile} · 只读 unified diff</summary>
          <div className="diff-lines">
            {lines.split("\n").map((line, i) => (
              <div
                key={i}
                className={
                  line.startsWith("+")
                    ? "added"
                    : line.startsWith("-")
                      ? "removed"
                      : line.startsWith("@@")
                        ? "hunk"
                        : ""
                }
              >
                <span className="diff-number">{i + 1}</span>
                <code>{line || " "}</code>
              </div>
            ))}
          </div>
        </details>
      )}
      <p className="hash-line">
        SHA256 <code>{candidate.patch_hash}</code>
      </p>
      <div className="patch-actions">
        <button
          className="button secondary"
          disabled={
            task.lifecycle !== "completed" || active || revalidate.isPending
          }
          onClick={() => revalidate.mutate()}
        >
          <FlaskConical size={16} />
          {active ? "验证已排队或运行中" : "重新独立验证"}
        </button>
        <span className="muted">每次验证保留独立记录。</span>
      </div>
      <ErrorBox error={revalidate.error} />
    </>
  );
}
function CheckSummary({
  title,
  result,
}: {
  title: string;
  result?: Execution;
}) {
  return (
    <div className="check-summary">
      <div>
        <h4>{title}</h4>
        <Status
          value={!result ? "not_run" : result.passed ? "passed" : "failed"}
          label={!result ? "未执行" : result.passed ? "通过" : "未通过"}
        />
      </div>
      {result && (
        <>
          <code>{result.command?.join(" ")}</code>
          {result.counts ? (
            <div className="test-counts">
              <span>
                <b>{result.counts.collected}</b>收集
              </span>
              <span className="green">
                <b>{result.counts.passed}</b>通过
              </span>
              <span>
                <b>{result.counts.failed}</b>失败
              </span>
              <span>
                <b>{result.counts.errors}</b>错误
              </span>
              <span>
                <b>{result.counts.skipped}</b>跳过
              </span>
            </div>
          ) : (
            <p>未取得测试收集记录</p>
          )}
          <small>
            exit {result.exit_code ?? "—"} · {result.reason ?? "等待执行"} ·{" "}
            {result.duration_seconds ?? "—"}s
          </small>
        </>
      )}
    </div>
  );
}
function ValidationPanel({ task }: { task: Task }) {
  return (
    <>
      <div className="content-section first">
        <h3>修复前的原始复现</h3>
        <p>原始快照与后续验证使用同一登记检查计划。</p>
        <CheckSummary title="原始目标测试" result={task.baseline?.[0]} />
        <CheckSummary title="原始完整回归" result={task.baseline?.[1]} />
      </div>
      {task.validations.length ? (
        task.validations.map((v: Validation) => (
          <div className="validation-record" key={v.id}>
            <div className="section-header">
              <h3>
                {v.kind === "independent"
                  ? "最终独立验证"
                  : v.kind === "revalidation"
                    ? "重新验证"
                    : "候选验证"}
                <small className="mono"> {v.id.slice(0, 8)}</small>
              </h3>
              <Status
                value={v.status}
                label={v.status === "failed" ? "验证未通过" : undefined}
              />
            </div>
            <CheckSummary title="修复后目标测试" result={v.target} />
            <CheckSummary title="修复后完整回归" result={v.regression} />
            {v.reasons?.map((r, i) => (
              <p className="validation-reason" key={i}>
                <TriangleAlert size={14} />
                {r}
              </p>
            ))}
            {v.regression?.environment_fingerprint && (
              <details className="fingerprint">
                <summary>查看环境与测试完整性指纹</summary>
                <dl>
                  <dt>环境指纹</dt>
                  <dd>{v.regression.environment_fingerprint}</dd>
                  <dt>原始测试文件</dt>
                  <dd>{v.regression.tests_sha256}</dd>
                  <dt>镜像</dt>
                  <dd>{v.regression.image_digest ?? "可信本机开发进程"}</dd>
                </dl>
              </details>
            )}
            <p className="scope-note">
              <ShieldCheck size={14} />
              {v.scope ?? "验证尚未完成"}
            </p>
          </div>
        ))
      ) : (
        <Empty
          title="尚无补丁验证记录"
          description="补丁产生后，平台会在全新副本上运行目标测试和完整回归。"
        />
      )}
    </>
  );
}
function StrategyPanel({ task }: { task: Task }) {
  return (
    <>
      <div className="notice info">
        <Activity size={18} />
        <div>
          <strong>
            {task.experiment_binding?.arm === "logs"
              ? "日志解释记录"
              : task.experiment_binding?.arm === "multi"
                ? "独立固定策略执行记录"
                : task.mode === "standard"
                  ? "固定策略执行记录"
                  : "自适应执行记录"}
          </strong>
          <p>
            {task.experiment_binding?.arm === "logs"
              ? "仅使用固定归档日志，未读取源码或执行代码，诊断准确率未评分。"
              : task.experiment_binding?.arm === "multi"
                ? "每次尝试使用相同初始材料，不共享上次补丁或验证反馈；选中候选仍另做独立验收。"
                : task.mode === "standard"
                  ? "标准模式的补丁修正不代表 Harness² 策略优化。只展示实际执行事实。"
                  : "Harness² 分别提出 INV / DOM 策略，再组合为 c1。k=1 直接采用唯一组合最终结果，修复结论由独立测试决定。"}
          </p>
        </div>
      </div>
      {task.harness && <HarnessDetails task={task} />}
      {task.experience && <TaskExperience key={task.id} task={task} />}
      {task.agent_tools && <AgentActions task={task} />}
      {(task.model_calls?.length ?? 0) > 0 && (
        <div className="content-section">
          <h3>模型调用记录</h3>
          <p>
            响应通过校验表示消息结构可用；工具与补丁分别校验，修复结果由独立测试判定。
          </p>
          {task.model_calls?.map((call, i) => (
            <div className="model-call" key={call.id}>
              <div>
                <strong>
                  调用 {i + 1} ·{" "}
                  {call.role === "harness_proposer"
                    ? "策略提议"
                    : call.role === "experience_reflection"
                      ? "经验反思"
                      : "诊断执行"}{" "}
                  {call.label}
                </strong>
                <span className="small-tag">
                  {(
                    {
                      running: "请求中",
                      completed: "响应校验通过",
                      rejected: "响应被拒绝",
                      failed: "调用失败",
                      interrupted: "执行中断",
                      cancelled: "已取消",
                      response_received: "响应已接收",
                    } as Record<string, string>
                  )[call.status] ?? call.status}
                </span>
              </div>
              <code>{call.response_model ?? call.model}</code>
              <small>
                {call.finished_at
                  ? `${Math.max(0, call.finished_at - call.started_at).toFixed(1)} 秒`
                  : "等待响应"}{" "}
                ·{" "}
                {call.usage_known
                  ? `${call.total_tokens?.toLocaleString()} tokens`
                  : `用量未知，预留 ${call.reserved_tokens.toLocaleString()} tokens`}
              </small>
              {call.error_code && (
                <small className="validation-reason">{call.error_code}</small>
              )}
              {call.rejection_reason && (
                <details>
                  <summary>查看响应校验原因</summary>
                  <pre className="log-excerpt">{call.rejection_reason}</pre>
                </details>
              )}
            </div>
          ))}
        </div>
      )}
      {task.rollouts.length ? (
        task.rollouts.map((r) => (
          <div className="rollout-card" key={r.id}>
            <div>
              <h3>{r.label}</h3>
              <Status value={r.status} />
            </div>
            <p className="hash-line">
              策略指纹 <code>{r.harness_hash}</code>
            </p>
            {r.harness_artifact_id && (
              <a
                className="text-link"
                href={artifactUrl(task.id, r.harness_artifact_id, true)}
              >
                <Download size={14} />
                下载此策略快照
              </a>
            )}
            {r.error && <p>{r.error}</p>}
          </div>
        ))
      ) : (
        <Empty
          title="尚无策略执行记录"
          description="模型开始实际诊断后会记录执行轮次和所用策略指纹。"
        />
      )}
    </>
  );
}
function HarnessDetails({ task }: { task: Task }) {
  const harness = task.harness!;
  return (
    <section className="content-section harness-details">
      <h3>Harness² 策略优化</h3>
      <p className="hash-line">
        上游版本 <code>{task.harness2_sha}</code>
      </p>
      <p>
        状态：{labels[harness.status] ?? harness.status} · k={harness.k} /{" "}
        {harness.mode} · {harness.bridge}
      </p>
      {harness.selected_label && (
        <div className="notice success">
          <ShieldCheck size={17} />
          <span>
            采用策略 {harness.selected_label}：{harness.selection_reason}
          </span>
        </div>
      )}
      {harness.stages.map((stage) => (
        <div className="rollout-card" key={stage.label}>
          <div>
            <h3>
              {stage.label} · {stage.dim}
            </h3>
            <span className="small-tag">
              {stage.fell_back
                ? "提议失败，继承原策略"
                : stage.kept_base
                  ? "保留原策略"
                  : "采用修改"}
            </span>
          </div>
          <p>{stage.notes || stage.error}</p>
        </div>
      ))}
      {harness.proposals.map((proposal) => (
        <HarnessDiff
          key={proposal.label}
          taskId={task.id}
          proposal={proposal}
        />
      ))}
      {harness.result_artifact_id && (
        <a
          className="button secondary"
          href={artifactUrl(task.id, harness.result_artifact_id, true)}
        >
          <Download size={15} />
          下载框架执行报告
        </a>
      )}
    </section>
  );
}
function HarnessDiff({
  taskId,
  proposal,
}: {
  taskId: string;
  proposal: NonNullable<Task["harness"]>["proposals"][number];
}) {
  const [open, setOpen] = useState(false);
  const diff = useQuery({
    queryKey: ["artifact", taskId, proposal.diff_artifact_id],
    queryFn: () => readArtifact(taskId, proposal.diff_artifact_id),
    enabled: open,
  });
  return (
    <article className="harness-diff">
      <h4>
        {proposal.label} · {proposal.changed ? "策略有修改" : "策略未修改"}
      </h4>
      <p>{proposal.notes}</p>
      <small className="hash-line">
        {proposal.before_hash.slice(0, 12)} → {proposal.after_hash.slice(0, 12)}
      </small>
      <button
        type="button"
        className="text-link"
        onClick={() => setOpen((v) => !v)}
      >
        {open ? "收起策略差异" : "查看策略差异"}
        <ChevronRight size={14} />
      </button>
      {open && (
        <>
          <ErrorBox error={diff.error} />
          <pre className="log-excerpt">{diff.data ?? "正在读取策略差异…"}</pre>
        </>
      )}
    </article>
  );
}
