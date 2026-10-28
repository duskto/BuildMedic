import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { z } from "zod";
import { api, artifactUrl } from "./api";
import { logIndexSchema, logWindowSchema } from "./contracts";

type Cursor = { line: number; column: number };
const kindName: Record<string, string> = {
  traceback: "Python traceback",
  exception: "异常",
  test_failure: "测试失败",
  dependency: "依赖错误",
  ci_error: "执行错误",
};

export function LogViewer({
  taskId,
  artifactId,
  index,
  initialLine = 1,
  highlightEnd = 0,
}: {
  taskId: string;
  artifactId: string;
  index?: z.infer<typeof logIndexSchema>;
  initialLine?: number;
  highlightEnd?: number;
}) {
  const [cursor, setCursor] = useState<Cursor>({
    line: Math.max(1, initialLine),
    column: 0,
  });
  const [history, setHistory] = useState<Cursor[]>([]);
  const [jump, setJump] = useState(String(Math.max(1, initialLine)));
  const page = useQuery({
    queryKey: ["log-lines", taskId, artifactId, cursor.line, cursor.column],
    queryFn: () =>
      api(
        `/tasks/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(artifactId)}/lines?start_line=${cursor.line}&start_column=${cursor.column}&max_lines=100`,
        logWindowSchema,
      ),
    staleTime: Infinity,
    retry: false,
  });
  const value = page.data;
  const matching = !index || !value || index.sha256 === value.sha256;
  const move = (next: Cursor) => {
    setHistory((previous) => [...previous, cursor]);
    setCursor(next);
    setJump(String(next.line));
  };
  return (
    <div className="log-viewer">
      {index && index.sections.length > 0 && (
        <>
          <p className="small-note">
            失败片段按原始行号定位。
            {index.sections_truncated &&
              "失败索引数量超出上限，仅列最早和最晚的片段；完整日志仍可按行读取。"}
          </p>
          <div className="log-section-links" aria-label="失败片段索引">
            {index.sections.map((section, i) => (
              <button
                type="button"
                className="text-link"
                key={i}
                onClick={() => move({ line: section.line_start, column: 0 })}
              >
                L{section.marker_line} ·{" "}
                {kindName[section.kind] ?? section.kind}
                {section.step ? ` · ${section.step}` : ""}
                {section.timestamp && <small>{section.timestamp}</small>}
              </button>
            ))}
          </div>
        </>
      )}
      <form
        className="log-jump"
        onSubmit={(event) => {
          event.preventDefault();
          const line = Number(jump);
          if (Number.isInteger(line) && line >= 1) move({ line, column: 0 });
        }}
      >
        <label>
          跳转行号{" "}
          <input
            aria-label="跳转日志行号"
            type="number"
            min={1}
            max={value?.total_lines || undefined}
            value={jump}
            onChange={(e) => setJump(e.target.value)}
            required
          />
        </label>
        <button
          type="submit"
          className="button secondary"
          disabled={page.isFetching}
        >
          跳转
        </button>
        <a className="text-link" href={artifactUrl(taskId, artifactId, true)}>
          下载完整脱敏日志
        </a>
      </form>
      {page.isPending && <p role="status">正在读取日志片段…</p>}
      {page.error && (
        <div role="alert" className="notice warning">
          {page.error instanceof Error ? page.error.message : "日志读取失败"}{" "}
          <button className="text-link" onClick={() => void page.refetch()}>
            重试读取
          </button>
        </div>
      )}
      {!matching && (
        <p role="alert" className="notice warning">
          日志索引与产物指纹不一致，请重新读取任务。
        </p>
      )}
      {matching && value && (
        <>
          <p className="small-note">
            {value.lines.length
              ? `L${value.lines[0].number}–L${value.lines[value.lines.length - 1].number} / 共 ${value.total_lines} 行`
              : "日志为空"}{" "}
            · 每页最多 100 行，超长行分段读取。
          </p>
          <div className="numbered-log" aria-label="分页日志内容">
            {value.lines.map((line) => (
              <div
                key={`${line.number}-${line.column}`}
                className={
                  line.number >= initialLine && line.number <= highlightEnd
                    ? "highlight"
                    : ""
                }
              >
                <span>
                  {line.number}
                  {line.column > 0 ? " 续" : ""}
                </span>
                <code>
                  {line.content.replace(/[\r\n]+$/, "") || " "}
                  {!line.complete && " [本行未完，下一页继续]"}
                </code>
              </div>
            ))}
          </div>
        </>
      )}
      <nav className="pagination" aria-label="日志分页">
        <button
          type="button"
          className="button secondary"
          disabled={history.length === 0 || page.isFetching}
          onClick={() => {
            const previous = history[history.length - 1];
            if (previous) {
              setHistory(history.slice(0, -1));
              setCursor(previous);
              setJump(String(previous.line));
            }
          }}
        >
          上一页日志
        </button>
        <button
          type="button"
          className="button secondary"
          disabled={
            !matching || !value || value.next_line === null || page.isFetching
          }
          onClick={() => {
            if (value?.next_line != null)
              move({ line: value.next_line, column: value.next_column ?? 0 });
          }}
        >
          下一页日志
        </button>
      </nav>
    </div>
  );
}
