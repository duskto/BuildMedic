import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { readArtifact } from "./api";
import type { Task } from "./contracts";

const names: Record<string, string> = {
  read_ci_log: "读取日志",
  read_repo_file: "读取源码",
  search_code: "检索代码",
  propose_patch: "提交候选补丁",
  run_check: "检查候选",
  read_check_result: "读取检查结果",
  submit_diagnosis: "交付诊断",
};
const statuses: Record<string, string> = {
  running: "执行中",
  completed: "已执行",
  rejected: "已拒绝",
  cancelled: "已取消",
  interrupted: "已中断",
};

function Action({
  task,
  action,
}: {
  task: Task;
  action: NonNullable<Task["agent_actions"]>[number];
}) {
  const [open, setOpen] = useState(false);
  const records = useQuery({
    queryKey: ["agent-action", task.id, action.id, action.result_artifact_id],
    enabled: open,
    queryFn: async () => ({
      request: await readArtifact(task.id, action.request_artifact_id),
      result: action.result_artifact_id
        ? await readArtifact(task.id, action.result_artifact_id)
        : "操作尚未结束，或在结果归档前中断。",
    }),
  });
  return (
    <article className="rollout-card agent-action">
      <div>
        <strong>
          {names[action.name] ?? action.name} · 第 {action.round} 轮
        </strong>
        <span className="small-tag">
          {statuses[action.status] ?? action.status}
        </span>
      </div>
      <p className="small-note">
        {action.name}
        {action.finished_at
          ? ` · ${Math.max(0, action.finished_at - action.started_at).toFixed(1)} 秒`
          : ""}
      </p>
      {action.error_code && (
        <p className="validation-reason">{action.error_code}</p>
      )}
      <button className="button secondary small" onClick={() => setOpen(!open)}>
        {open ? "收起工具记录" : "查看工具记录"}
      </button>
      {open && (
        <>
          {records.error && <p role="alert">{records.error.message}</p>}
          {records.isPending ? (
            <p role="status">正在读取…</p>
          ) : (
            <>
              <h4>请求参数</h4>
              <pre className="log-excerpt">{records.data?.request}</pre>
              <h4>平台返回</h4>
              <pre className="log-excerpt">{records.data?.result}</pre>
            </>
          )}
        </>
      )}
    </article>
  );
}

export function AgentActions({ task }: { task: Task }) {
  return (
    <section className="content-section">
      <h3>Agent 工具调用</h3>
      <p>
        只展示平台实际执行或拒绝的操作。候选检查之后，平台仍会对最终补丁独立验收。
      </p>
      {!task.agent_actions?.length && <p>尚未收到工具调用。</p>}
      {task.agent_actions?.map((action) => (
        <Action key={action.id} task={task} action={action} />
      ))}
    </section>
  );
}
