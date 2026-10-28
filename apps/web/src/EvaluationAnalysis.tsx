import { useQuery } from "@tanstack/react-query";
import { z } from "zod";
import { api } from "./api";

const distribution = z.object({
  n: z.number(),
  mean: z.number().nullable(),
  minimum: z.number().nullable(),
  maximum: z.number().nullable(),
  sample_stddev: z.number().nullable(),
});
const analysisSchema = z.object({
  source_report_sha256: z.string(),
  complete: z.boolean(),
  environment_consistent: z.boolean().nullable(),
  repeat_results: z.array(
    z.object({
      mode: z.string(),
      repeat: z.number(),
      planned: z.number(),
      finished: z.number(),
      public_verified: z.number(),
      public_rate: z.number().nullable(),
      calls: z.number(),
      tokens: z.number().nullable(),
      wall_seconds: z.number(),
    }),
  ),
  variation: z.array(
    z.object({
      mode: z.string(),
      planned_repeats: z.number(),
      complete_repeats: z.number(),
      public_rate: distribution,
      tokens: distribution,
    }),
  ),
  pairs: z.array(
    z.object({
      reference: z.string(),
      compared: z.string(),
      planned: z.number(),
      paired: z.number(),
      pending: z.number(),
      compared_only: z.number(),
      reference_only: z.number(),
      both_verified: z.number(),
      neither_verified: z.number(),
      public_rate_delta: z.number().nullable(),
      calls_delta: z.number(),
      tokens_delta: z.number().nullable(),
      unknown_usage_pairs: z.number(),
      wall_seconds_delta: z.number(),
      cost_usd_delta: z.number().nullable(),
      unknown_cost_pairs: z.number(),
    }),
  ),
});
const modeName = (mode: string) =>
  ({ standard: "固定策略", multi: "独立多次", adaptive: "Harness²" })[mode] ??
  mode;
const percent = (value: number | null) =>
  value === null ? "—" : `${(value * 100).toFixed(1)}%`;
const signed = (value: number, decimals = 0) =>
  `${value > 0 ? "+" : ""}${value.toFixed(decimals)}`;

export function EvaluationAnalysis({
  id,
  sourceHash,
}: {
  id: string;
  sourceHash: string;
}) {
  const query = useQuery({
    queryKey: ["evaluation-analysis", id, sourceHash],
    queryFn: () => api(`/experiments/${id}/analysis`, analysisSchema),
    staleTime: Infinity,
    retry: false,
  });
  const result = query.data;
  return (
    <section className="panel padded">
      <h2>重复运行与配对比较</h2>
      <p className="small-note">
        按同一案例、同一轮次配对；差值为后者减前者。仅描述本批公开检查结果，同案例重复不视为新的独立样本，不能据此推断显著性或泛化收益。日志组诊断未评分，不参与修复比较。
      </p>
      {query.isPending && <p role="status">正在计算重复与配对统计…</p>}
      {query.error && (
        <p role="alert" className="notice warning">
          分析暂不可用：
          {query.error instanceof Error ? query.error.message : "请求失败"}
        </p>
      )}
      {result && result.source_report_sha256 !== sourceHash && (
        <p role="status">报告正在更新，等待下一次刷新后核对分析。</p>
      )}
      {result && result.source_report_sha256 === sourceHash && (
        <>
          {!result.complete && (
            <p className="notice info">
              本批尚未完整结束。未归档项保留为待配对；通过率差仅在全部配对完成后显示。
            </p>
          )}
          {result.environment_consistent !== true && (
            <p className="notice warning">
              执行环境尚未确认一致，请结合逐项证据审阅。
            </p>
          )}
          <h3>各轮结果</h3>
          <p className="small-note">窄屏可横向滚动表格，查看全部结果与消耗。</p>
          <div
            className="table-scroll"
            tabIndex={0}
            role="region"
            aria-label="各轮结果表格，可横向滚动"
          >
            <table className="analysis-table">
              <thead>
                <tr>
                  <th>策略 / 轮次</th>
                  <th>归档 / 登记</th>
                  <th>公开通过</th>
                  <th>模型调用 / Token</th>
                  <th>执行耗时</th>
                </tr>
              </thead>
              <tbody>
                {result.repeat_results.map((r) => (
                  <tr key={`${r.mode}-${r.repeat}`}>
                    <td>
                      {modeName(r.mode)} · 第 {r.repeat} 轮
                    </td>
                    <td>
                      {r.finished} / {r.planned}
                    </td>
                    <td>
                      {r.public_verified} / {r.planned} ·{" "}
                      {r.public_rate === null
                        ? "待完整归档"
                        : percent(r.public_rate)}
                    </td>
                    <td>
                      {r.calls} /{" "}
                      {r.tokens === null
                        ? "Token 不完整或尚无记录"
                        : r.tokens.toLocaleString()}
                    </td>
                    <td>{r.wall_seconds.toFixed(1)} 秒</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {result.variation.map((v) => (
            <p className="small-note" key={v.mode}>
              {modeName(v.mode)}：完整轮次 {v.complete_repeats} /{" "}
              {v.planned_repeats}；通过率均值 {percent(v.public_rate.mean)}
              ，范围 {percent(v.public_rate.minimum)} 至{" "}
              {percent(v.public_rate.maximum)}，样本标准差{" "}
              {v.public_rate.sample_stddev === null
                ? "至少需两轮完整记录"
                : `${(v.public_rate.sample_stddev * 100).toFixed(1)} 个百分点`}
              。用量完整轮次 {v.tokens.n}，Token 均值{" "}
              {v.tokens.mean?.toFixed(0) ?? "未知"}。
            </p>
          ))}
          <h3>同案例配对</h3>
          <div
            className="table-scroll"
            tabIndex={0}
            role="region"
            aria-label="同案例配对表格，可横向滚动"
          >
            <table className="analysis-table analysis-pairs">
              <thead>
                <tr>
                  <th>前者 → 后者</th>
                  <th>配对 / 登记</th>
                  <th>仅后者 / 仅前者通过</th>
                  <th>共同通过 / 共同未通过</th>
                  <th>通过率差</th>
                  <th>调用 / Token 差</th>
                  <th>耗时 / 费用差</th>
                </tr>
              </thead>
              <tbody>
                {result.pairs.map((p) => (
                  <tr key={`${p.reference}-${p.compared}`}>
                    <td>
                      {modeName(p.reference)} → {modeName(p.compared)}
                    </td>
                    <td>
                      {p.paired} / {p.planned}；待配对 {p.pending}
                    </td>
                    <td>
                      {p.compared_only} / {p.reference_only}
                    </td>
                    <td>
                      {p.both_verified} / {p.neither_verified}
                    </td>
                    <td>
                      {p.public_rate_delta === null
                        ? "待完整配对"
                        : `${signed(p.public_rate_delta * 100, 1)} 个百分点`}
                    </td>
                    <td>
                      {p.paired
                        ? `${signed(p.calls_delta)} / ${p.tokens_delta === null ? "未知" : signed(p.tokens_delta)}`
                        : "—"}
                    </td>
                    <td>
                      {p.paired
                        ? `${signed(p.wall_seconds_delta, 1)} 秒 / ${p.cost_usd_delta === null ? "未知" : `$${signed(p.cost_usd_delta, 4)}`}`
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="small-note">
            消耗差累计所有已配对项，包含失败、预算停止及取消项；任一配对用量或费用不完整时，相应总差为未知。波动仅汇总完整轮次，少于两轮时标准差不计算。
          </p>
          <a
            className="text-link"
            href={`/api/v1/experiments/${encodeURIComponent(id)}/analysis/report`}
          >
            下载可复算分析
          </a>
        </>
      )}
    </section>
  );
}
