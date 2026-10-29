import { z } from "zod";
import { Link } from "react-router-dom";

export const frozenGuidanceSchema = z.object({
  sha256: z.string(),
  version_ids: z.array(z.string()),
  entries: z.array(
    z.object({
      id: z.string(),
      source_case_id: z.string(),
      version: z.number(),
      cue: z.string(),
      lesson: z.string(),
      model: z.string(),
    }),
  ),
  training_usage: z.object({
    tasks: z.number(),
    calls: z.number(),
    tokens: z.number(),
    unknown_usage_tasks: z.number(),
    cost_usd: z.number().nullable(),
  }),
});

export function FrozenGuidancePanel({
  value,
}: {
  value: z.infer<typeof frozenGuidanceSchema>;
}) {
  return (
    <section className="panel padded">
      <h2>冻结开发指导</h2>
      <p>
        三个修复组使用相同的只读指导；日志组不接收指导。每个任务仍独立产生证据，不自动整理、发布或累积新经验。
      </p>
      <p className="small-note break-all">
        整批指导指纹：<code>{value.sha256}</code>
      </p>
      {value.entries.length === 0 ? (
        <p>已冻结空指导，本批没有使用已发布经验。</p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>开发来源</th>
                <th>版本</th>
                <th>触发线索与指导</th>
                <th>来源模型</th>
              </tr>
            </thead>
            <tbody>
              {value.entries.map((entry) => (
                <tr key={entry.id}>
                  <td>{entry.source_case_id}</td>
                  <td>
                    <Link to={`/experience/${entry.id}`}>v{entry.version}</Link>
                  </td>
                  <td>
                    {entry.cue}
                    <br />
                    {entry.lesson}
                  </td>
                  <td>{entry.model}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p>
        训练来源共 {value.training_usage.tasks} 个任务，
        {value.training_usage.calls} 次模型调用，
        {value.training_usage.tokens.toLocaleString()} 已知 Token；费用
        {value.training_usage.cost_usd === null
          ? "未知"
          : ` $${value.training_usage.cost_usd.toFixed(4)}`}
        ，用量不完整任务 {value.training_usage.unknown_usage_tasks} 个。
      </p>
      <p className="small-note">
        训练消耗单列，不计入本批修复预算。只使用明确选择的开发版本，后续发布与撤销不改写本批输入；当前记录不代表指导已带来收益。
      </p>
    </section>
  );
}
