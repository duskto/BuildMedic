import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";
import { api } from "./api";
import { healthSchema } from "./contracts";

const scoreSchema = z.object({
  id: z.string(),
  status: z.enum(["pending", "running", "completed", "failed"]),
  experiment_id: z.string(),
  report_sha256: z.string(),
  suite_sha256: z.string(),
  error_code: z.string().nullable(),
  scope: z.string(),
  summary: z.array(
    z.object({
      mode: z.string(),
      repair_applicable: z.boolean(),
      planned: z.number(),
      behavior_verified: z.number(),
      evaluated: z.number(),
      evaluation_errors: z.number(),
      not_reproduced: z.number(),
      no_candidate: z.number(),
      false_positives: z.number(),
      false_positive_denominator: z.number(),
      new_regressions: z.number(),
      regression_denominator: z.number(),
    }),
  ),
  receipt: z
    .object({
      image_digest: z.string(),
      status: z.string(),
      signature: z.string(),
    })
    .passthrough()
    .nullable(),
});
const names: Record<string, string> = {
  standard: "单次固定策略",
  multi: "独立多次策略",
  adaptive: "Harness²",
};

export function IndependentScore({
  id,
  complete,
  protocol,
}: {
  id: string;
  complete: boolean;
  protocol?: string;
}) {
  const cache = useQueryClient();
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => api("/health", healthSchema),
  });
  const score = useQuery({
    queryKey: ["independent-score", id],
    queryFn: () => api(`/experiments/${id}/score`, scoreSchema.nullable()),
    refetchInterval: (query) =>
      query.state.data &&
      ["pending", "running"].includes(query.state.data.status)
        ? 2000
        : false,
  });
  const start = useMutation({
    mutationFn: () =>
      api(`/experiments/${id}/score`, scoreSchema, { method: "POST" }),
    onSuccess: (data) => cache.setQueryData(["independent-score", id], data),
  });
  const value = score.data;
  const error = score.error ?? start.error;
  return (
    <section className="panel padded">
      <h2>独立行为评测</h2>
      <p>
        实验全部结束后，另由评分服务检查冻结补丁。私有断言不会提供给诊断模型；此处只展示评分回执，原公开结果保持不变。当前范围为公开开发案例；诊断准确率通过单独的人工审核记录，隔离测试集尚未评分。
      </p>
      {error && (
        <p role="alert" className="notice warning">
          {error.message}
        </p>
      )}
      {!value && (
        <>
          <button
            className="button secondary"
            disabled={
              !complete ||
              protocol !== "2.0" ||
              !health.data?.scoring_ready ||
              start.isPending ||
              score.isPending
            }
            onClick={() => start.mutate()}
          >
            运行独立行为评测
          </button>
          <p className="small-note">
            {!complete
              ? "请先完成整批实验。"
              : protocol !== "2.0"
                ? "旧版两组清单不参与此评分。"
                : !health.data?.scoring_ready
                  ? "尚未配置独立评分服务。"
                  : "将运行额外容器检查，不请求模型；每份冻结实验仅保留一次评分。"}
          </p>
        </>
      )}
      {value && (
        <>
          <p role="status">
            {
              (
                {
                  pending: "等待评分",
                  running: "评分中",
                  completed: "评分已完成",
                  failed: "评分未完成",
                } as Record<string, string>
              )[value.status]
            }
          </p>
          {value.error_code && (
            <p className="notice warning">
              评分服务暂不可用或回执未通过校验。已有请求已保留。
            </p>
          )}
          {value.status === "completed" && (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>修复组</th>
                    <th>行为与公开回归通过 / 总数</th>
                    <th>实际评分</th>
                    <th>误报 / 已评分公开修复</th>
                    <th>新增行为回归 / 已评分补丁</th>
                  </tr>
                </thead>
                <tbody>
                  {value.summary
                    .filter((s) => s.repair_applicable)
                    .map((s) => (
                      <tr key={s.mode}>
                        <td>{names[s.mode]}</td>
                        <td>
                          {s.behavior_verified} / {s.planned}
                        </td>
                        <td>
                          {s.evaluated}；异常 {s.evaluation_errors}，未复现{" "}
                          {s.not_reproduced}，无候选 {s.no_candidate}
                        </td>
                        <td>
                          {s.false_positives} / {s.false_positive_denominator}
                        </td>
                        <td>
                          {s.new_regressions} / {s.regression_denominator}
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="small-note">
            未评分和环境异常不计为误报；分母为零时比例不可计算。日志组不参与修复率。
          </p>
          <p>
            <a
              className="text-link"
              href={`/api/v1/experiments/${encodeURIComponent(id)}/score/report`}
            >
              下载独立评分回执
            </a>
          </p>
          <details>
            <summary>评分来源与指纹</summary>
            <p className="break-all">公开报告：{value.report_sha256}</p>
            <p className="break-all">评分包：{value.suite_sha256}</p>
            <p className="break-all">
              镜像：{value.receipt?.image_digest ?? "尚无回执"}
            </p>
          </details>
        </>
      )}
    </section>
  );
}
