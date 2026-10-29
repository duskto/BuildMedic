import { useQuery } from "@tanstack/react-query";
import { z } from "zod";
import { api } from "./api";

const scoreSchema = z.object({
  id: z.string(),
  status: z.enum(["awaiting_review", "completed"]),
  report_sha256: z.string(),
  rubric_sha256: z.string(),
  scope: z.string(),
  summary: z.array(
    z.object({
      mode: z.string(),
      planned: z.number(),
      delivered: z.number(),
      missing: z.number(),
      reviewed: z.number(),
      unreviewed: z.number(),
      root_cause_correct: z.number(),
      location_correct: z.number(),
      both_correct: z.number(),
      diagnosis_accuracy: z.number().min(0).max(1).nullable(),
    }),
  ),
});

const names: Record<string, string> = {
  logs: "仅日志诊断",
  standard: "单次固定策略",
  multi: "独立多次策略",
  adaptive: "Harness²",
};

export function DiagnosisScore({
  id,
  complete,
  reportHash,
}: {
  id: string;
  complete: boolean;
  reportHash: string;
}) {
  const score = useQuery({
    queryKey: ["diagnosis-score", id],
    queryFn: () =>
      api(`/experiments/${id}/diagnosis-score`, scoreSchema.nullable()),
    enabled: complete,
  });
  const value = score.data;
  const matches = value?.report_sha256 === reportHash;
  return (
    <section className="panel padded">
      <h2>独立诊断审核</h2>
      <p>
        独立审核人分别判断根因与定位。审核单隐藏实验组别、模型和修复结果；审核完成后导入签名回执，评分不提供给诊断模型。
      </p>
      <p className="small-note">
        准确率 = 根因与定位同时正确的诊断 /
        全部计划任务。缺失诊断保留在分母，未完成审核时显示未评分。
      </p>
      {score.error && (
        <p role="alert" className="notice warning">
          {score.error.message}
        </p>
      )}
      {!complete ? (
        <p>请先完成整批实验。</p>
      ) : (
        <>
          <button
            className="button secondary"
            disabled={score.isFetching}
            onClick={() => void score.refetch()}
          >
            刷新审核结果
          </button>
          {!value && !score.error && (
            <p role="status">
              {score.isPending
                ? "读取审核结果…"
                : "尚无独立人工审核，诊断准确率未评分。"}
            </p>
          )}
        </>
      )}
      {value && !matches && (
        <p role="alert" className="notice warning">
          审核回执与当前报告指纹不一致，请重新载入实验。
        </p>
      )}
      {value && matches && (
        <>
          <p role="status">
            {value.status === "completed"
              ? "人工审核已封存"
              : "等待独立人工审核；准确率未评分"}
          </p>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>实验组</th>
                  <th>已审核 / 已交付</th>
                  <th>缺失诊断</th>
                  <th>根因正确</th>
                  <th>定位正确</th>
                  <th>诊断准确率（同时正确 / 总数）</th>
                </tr>
              </thead>
              <tbody>
                {value.summary.map((row) => (
                  <tr key={row.mode}>
                    <td>{names[row.mode] ?? row.mode}</td>
                    <td>
                      {row.reviewed} / {row.delivered}
                    </td>
                    <td>{row.missing}</td>
                    <td>
                      {value.status === "completed"
                        ? row.root_cause_correct
                        : "未评分"}
                    </td>
                    <td>
                      {value.status === "completed"
                        ? row.location_correct
                        : "未评分"}
                    </td>
                    <td>
                      {row.diagnosis_accuracy === null
                        ? "未评分"
                        : `${(100 * row.diagnosis_accuracy).toFixed(1)}%（${row.both_correct} / ${row.planned}）`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="small-note">
            审核规则指纹：
            <code className="break-all">{value.rubric_sha256}</code>
          </p>
          <a
            className="button secondary"
            href={`/api/v1/experiments/${id}/diagnosis-score/report`}
          >
            下载诊断审核回执
          </a>
        </>
      )}
    </section>
  );
}
