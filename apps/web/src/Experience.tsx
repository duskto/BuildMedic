import { useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";
import { api, artifactUrl } from "./api";
import {
  experienceDetailSchema,
  experiencePageSchema,
  experienceVersionSchema,
} from "./contracts";
import type { Task } from "./contracts";

function Failure({ error }: { error: unknown }) {
  return error ? (
    <p role="alert" className="notice warning">
      {error instanceof Error ? error.message : "请求失败，请重试"}
    </p>
  ) : null;
}

const reflectionLabels: Record<string, string> = {
  pending: "等待反思",
  running: "反思中",
  awaiting_validation: "等待最终独立验证",
  available: "草稿已通过独立验证",
  no_entry: "无足够可迁移经验",
  rejected: "反思校验未通过",
  unverified: "验证未通过",
  interrupted: "反思中断",
  cancelled: "任务已取消",
};

export function TaskExperience({ task }: { task: Task }) {
  const experience = task.experience!;
  const cache = useQueryClient();
  const key = useRef(crypto.randomUUID());
  const publish = useMutation({
    mutationFn: () =>
      api(`/tasks/${task.id}/experience`, experienceVersionSchema, {
        method: "POST",
        headers: { "Idempotency-Key": key.current },
      }),
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: ["task", task.id] });
      void cache.invalidateQueries({ queryKey: ["experience"] });
    },
  });
  const [showSnapshot, setShowSnapshot] = useState(false);
  const snapshot = useQuery({
    queryKey: ["experience-snapshot", experience.snapshot_id],
    enabled: showSnapshot,
    queryFn: () =>
      api(
        `/experience/snapshots/${experience.snapshot_id}`,
        z.object({
          content_hash: z.string(),
          content: z.record(z.unknown()),
        }),
      ),
  });
  return (
    <section className="content-section experience-section">
      <h3>任务经验</h3>
      <p>
        输入领域：{experience.domain} · 提交时库版本：v{experience.head_version}{" "}
        · 仅供本地开发参考
      </p>
      <p className="small-note">
        经验输入已冻结，重试复用此快照。经验建议不改变平台权限和验收规则。
      </p>
      <p>
        快照指纹 <code className="fingerprint">{experience.snapshot_hash}</code>
      </p>
      {experience.entries.length ? (
        experience.entries.map((e) => (
          <p key={e.id}>
            <Link to={`/experience/${e.id}`}>
              已绑定 v{e.version} · {e.cue}
            </Link>
          </p>
        ))
      ) : (
        <p>空经验快照，本任务未读取历史经验。</p>
      )}
      <button
        type="button"
        className="button secondary small"
        onClick={() => setShowSnapshot(!showSnapshot)}
      >
        {showSnapshot ? "收起输入快照" : "查看输入快照"}
      </button>
      {showSnapshot && (
        <>
          <Failure error={snapshot.error} />
          <pre className="log-excerpt">
            {snapshot.data ? JSON.stringify(snapshot.data, null, 2) : "读取中…"}
          </pre>
        </>
      )}
      <h4>
        {reflectionLabels[experience.reflection_status] ??
          experience.reflection_status}
      </h4>
      {experience.reason && <p>{experience.reason}</p>}
      {experience.draft && (
        <div className="experience-draft">
          <strong>{experience.draft.cue}</strong>
          <p>{experience.draft.lesson}</p>
          <p className="small-note">
            证据：{experience.draft.evidence_ids.join("、")}
            。通过验证的是来源补丁；尚未评测经验在其他任务上的效果。
          </p>
          <a
            className="text-link"
            href={artifactUrl(task.id, experience.draft.artifact_id, true)}
          >
            下载完整经验草稿
          </a>
        </div>
      )}
      {experience.published_version_id ? (
        <Link
          className="button secondary"
          to={`/experience/${experience.published_version_id}`}
        >
          查看已发布经验
        </Link>
      ) : (
        experience.reflection_status === "available" &&
        task.lifecycle === "completed" &&
        task.outcome === "repaired" && (
          <button
            className="button primary"
            disabled={publish.isPending}
            onClick={() => publish.mutate()}
          >
            {publish.isPending ? "发布中…" : "发布到我的经验库"}
          </button>
        )
      )}
      <Failure error={publish.error} />
    </section>
  );
}

export function ExperienceLibrary() {
  const [domain, setDomain] = useState("");
  const [page, setPage] = useState(1);
  const versions = useQuery({
    queryKey: ["experience", domain, page],
    queryFn: () =>
      api(
        `/experience?page=${page}&domain=${encodeURIComponent(domain)}`,
        experiencePageSchema,
      ),
  });
  return (
    <>
      <div className="page-title">
        <div>
          <div className="eyebrow">EXPERIENCE LIBRARY</div>
          <h1>任务经验库</h1>
          <p>当前会话发布的同域策略与证据。跨任务效果尚未评测。</p>
        </div>
      </div>
      <div className="notice info">
        新任务仅在主动开启经验库时，绑定最新兼容版本。撤销影响后续快照，已创建任务的输入保持固定。
      </div>
      <label className="experience-filter">
        筛选领域
        <input
          value={domain}
          placeholder="例如：业务逻辑"
          onChange={(e) => {
            setDomain(e.target.value);
            setPage(1);
          }}
        />
      </label>
      <Failure error={versions.error} />
      {versions.isPending && <p role="status">正在读取经验…</p>}
      {versions.data?.total === 0 && (
        <div className="panel experience-section">
          <h2>还没有发布的经验</h2>
          <p>
            创建自适应任务并启用任务反思，独立验证通过后，在策略记录中查看并发布草稿。
          </p>
          <Link className="button primary" to="/tasks/new">
            创建诊断
          </Link>
        </div>
      )}
      {versions.data?.items.map((v) => (
        <article className="panel experience-section" key={v.id}>
          <p className="eyebrow">
            {v.domain} · v{v.version} ·{" "}
            {v.status === "active" ? "可用于新任务" : "已撤销"}
          </p>
          <h2>
            <Link to={`/experience/${v.id}`}>{v.cue}</Link>
          </h2>
          <p>{v.lesson}</p>
          <Link
            className="text-link"
            to={`/tasks/${v.source_task_id}?tab=strategy`}
          >
            查看来源任务与独立验证
          </Link>
        </article>
      ))}
      {versions.data && versions.data.total > 20 && (
        <div className="pagination">
          <button disabled={page === 1} onClick={() => setPage(page - 1)}>
            上一页
          </button>
          <span>第 {page} 页</span>
          <button
            disabled={page * 20 >= versions.data.total}
            onClick={() => setPage(page + 1)}
          >
            下一页
          </button>
        </div>
      )}
    </>
  );
}

export function ExperienceDetail() {
  const { id = "" } = useParams();
  const cache = useQueryClient();
  const query = useQuery({
    queryKey: ["experience-version", id],
    queryFn: () => api(`/experience/${id}`, experienceDetailSchema),
  });
  const revoke = useMutation({
    mutationFn: () =>
      api(`/experience/${id}/revoke`, experienceVersionSchema, {
        method: "POST",
      }),
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: ["experience-version", id] });
      void cache.invalidateQueries({ queryKey: ["experience"] });
    },
  });
  const v = query.data;
  return (
    <>
      <Link className="back-link" to="/experience">
        返回经验库
      </Link>
      <Failure error={query.error} />
      {query.isPending && <p role="status">正在读取经验…</p>}
      {v && (
        <article className="panel experience-section">
          <p className="eyebrow">
            {v.domain} · v{v.version} ·{" "}
            {v.status === "active" ? "可用于新任务" : "已撤销"}
          </p>
          <h1>{v.cue}</h1>
          <p>{v.lesson}</p>
          <div className="notice info">
            开发经验，尚未评测跨任务效果。来源补丁已通过登记环境的独立验证。
          </div>
          <p>改进摘要：{v.content.edits || "未提供"}</p>
          <p>证据：{v.evidence_ids.join("、")}</p>
          <Link
            className="button secondary"
            to={`/tasks/${v.source_task_id}?tab=strategy`}
          >
            查看来源任务
          </Link>
          <dl className="experience-facts">
            <dt>候选 / 独立验证</dt>
            <dd>
              {v.content.candidate_id} / {v.content.validation_id}
            </dd>
            <dt>生成模型</dt>
            <dd>{v.content.response_model ?? v.content.model}</dd>
            <dt>内容指纹</dt>
            <dd>
              <code>{v.content_hash}</code>
            </dd>
            <dt>策略指纹</dt>
            <dd>
              <code>{v.content.harness_hash}</code>
            </dd>
            <dt>补丁指纹</dt>
            <dd>
              <code>{v.content.patch_hash}</code>
            </dd>
            <dt>上游版本</dt>
            <dd>
              <code>{v.content.harness2_sha}</code>
            </dd>
            <dt>来源输入快照</dt>
            <dd>
              <code>{v.content.input_snapshot_id}</code>
            </dd>
            <dt>输入快照指纹</dt>
            <dd>
              <code>{v.content.input_snapshot_hash}</code>
            </dd>
            <dt>父经验版本</dt>
            <dd>
              {v.content.parent_version_ids.length
                ? v.content.parent_version_ids.map((parent) => (
                    <Link key={parent} to={`/experience/${parent}`}>
                      {parent}{" "}
                    </Link>
                  ))
                : "空库起步"}
            </dd>
          </dl>
          <details>
            <summary>查看完整采纳策略</summary>
            {Object.entries(v.content.tree).map(([path, text]) => (
              <div key={path}>
                <h4>{path}</h4>
                <pre className="log-excerpt">{text}</pre>
              </div>
            ))}
          </details>
          {v.status === "active" && (
            <button
              className="button secondary"
              disabled={revoke.isPending}
              onClick={() => revoke.mutate()}
            >
              {revoke.isPending ? "撤销中…" : "撤销此版本，仅影响后续快照"}
            </button>
          )}
          <Failure error={revoke.error} />
        </article>
      )}
    </>
  );
}
