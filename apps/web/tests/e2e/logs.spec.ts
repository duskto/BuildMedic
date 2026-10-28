import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";

function task(indexed = true) {
  return {
    id: "log-fixture",
    title: "长日志测试任务",
    repo: "owner/repo",
    source_type: "github_actions",
    case_id: null,
    head_sha: "a".repeat(40),
    run_id: 42,
    run_attempt: 2,
    job_id: 7,
    profile_version: null,
    execution_label: "明确合成日志",
    parent_task_id: null,
    mode: "standard",
    lifecycle: "completed",
    phase: "report",
    phase_history: ["ingest", "diagnose", "report"],
    outcome: "diagnosed_only",
    validation_status: "not_run",
    reproduction_status: "not_run",
    message: "仅日志诊断",
    state_version: 1,
    last_event_seq: 1,
    created_at: 1,
    usage: { calls: 0, tokens: 0, cost_usd: null, usage_known: true },
    limits: { max_wall_seconds: 600, max_model_calls: 8, max_tokens: 48000 },
    evidence: [
      {
        id: "ev-log",
        kind: "ci_log",
        artifact_id: "log-artifact",
        path: null,
        line_start: indexed ? 116 : 1,
        line_end: indexed ? 140 : 1,
        excerpt: "AssertionError: boundary",
        log_context_artifact_ids: ["context-artifact"],
        ...(indexed
          ? {
              log_index: {
                schema_version: "1.0",
                sha256: "b".repeat(64),
                total_lines: 500,
                size_bytes: 20000,
                sections_truncated: false,
                sections: [
                  {
                    kind: "exception",
                    priority: 95,
                    line_start: 116,
                    line_end: 140,
                    marker_line: 120,
                    step: "Run pytest",
                    timestamp: "2026-10-28T01:00:00Z",
                  },
                ],
              },
            }
          : {}),
      },
    ],
    candidates: [],
    validations: [],
    executions: [],
    rollouts: [],
    diagnosis: null,
    artifacts: [],
  };
}

async function open(page: Page, indexed = true) {
  await page.route("**/api/v1/tasks/log-fixture", (route) =>
    route.fulfill({ json: { data: task(indexed), request_id: "fixture" } }),
  );
  await page.goto("/tasks/log-fixture?tab=evidence");
  await page.getByRole("button", { name: "展开完整上下文" }).click();
}

function window(line: number) {
  const last = Math.min(line + 99, 500);
  return {
    artifact_id: "log-artifact",
    sha256: "b".repeat(64),
    total_lines: 500,
    lines: Array.from({ length: last - line + 1 }, (_, i) => ({
      number: line + i,
      content:
        line + i === 120 ? "AssertionError: boundary\n" : `line ${line + i}\n`,
      column: 0,
      complete: true,
    })),
    next_line: last < 500 ? last + 1 : null,
    next_column: last < 500 ? 0 : null,
  };
}

for (const source of ["cache", "network"] as const) {
  test(`CI log origin shows ${source} and a downloadable source record`, async ({
    page,
  }) => {
    const fixture = task();
    await page.route("**/api/v1/tasks/log-fixture", (route) =>
      route.fulfill({
        json: {
          data: {
            ...fixture,
            evidence: fixture.evidence.map((e) => ({
              ...e,
              log_origin: {
                source,
                cached_at: 1793150000,
                expires_at: 1793236400,
                artifact_id: "origin-artifact",
              },
            })),
          },
          request_id: "fixture",
        },
      }),
    );
    await page.goto("/tasks/log-fixture?tab=evidence");
    await expect(
      page.getByText(
        source === "cache" ? /复用本会话缓存/ : /本次从 GitHub 下载/,
      ),
    ).toBeVisible();
    await expect(
      page.getByRole("link", { name: "下载来源记录" }),
    ).toHaveAttribute(
      "href",
      "/api/v1/tasks/log-fixture/artifacts/origin-artifact?download=true",
    );
  });
}

test("log navigation preserves original lines, jumps to failures and never fetches the whole artifact", async ({
  page,
}) => {
  const requests: number[] = [];
  const errors: string[] = [];
  let wholeReads = 0;
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route(
    "**/api/v1/tasks/log-fixture/artifacts/log-artifact",
    (route) => {
      wholeReads++;
      return route.fulfill({ body: "unexpected whole read" });
    },
  );
  await page.route(
    "**/api/v1/tasks/log-fixture/artifacts/log-artifact/lines?*",
    (route) => {
      const line = Number(
        new URL(route.request().url()).searchParams.get("start_line"),
      );
      requests.push(line);
      return route.fulfill({
        json: { data: window(line), request_id: "fixture" },
      });
    },
  );
  await open(page);
  await expect(
    page.getByText("L116–L215 / 共 500 行", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByLabel("分页日志内容").locator(":scope > div"),
  ).toHaveCount(100);
  await expect(page.getByText("2026-10-28T01:00:00Z")).toBeVisible();
  await expect(
    page.getByRole("link", { name: "下载模型使用的日志片段 1" }),
  ).toHaveAttribute(
    "href",
    "/api/v1/tasks/log-fixture/artifacts/context-artifact?download=true",
  );
  await page.getByRole("button", { name: "下一页日志" }).click();
  await expect(page.getByText(/L216–L315/)).toBeVisible();
  await page.getByRole("button", { name: "上一页日志" }).click();
  await expect(page.getByText(/L116–L215/)).toBeVisible();
  await page.getByRole("spinbutton", { name: "跳转日志行号" }).fill("450");
  await page.getByRole("button", { name: "跳转", exact: true }).click();
  await expect(page.getByText(/L450–L500/)).toBeVisible();
  await expect(page.getByRole("button", { name: "下一页日志" })).toBeDisabled();
  await page.getByRole("button", { name: /^L120 · 异常 · Run pytest/ }).click();
  await expect(page.getByText(/L116–L215/)).toBeVisible();
  await expect(
    page.getByRole("link", { name: "下载完整脱敏日志" }),
  ).toHaveAttribute(
    "href",
    "/api/v1/tasks/log-fixture/artifacts/log-artifact?download=true",
  );
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(wholeReads).toBe(0);
  expect(requests).toContain(450);
  expect(errors).toEqual([]);
});

test("legacy logs support long-line continuation without losing the column cursor", async ({
  page,
}) => {
  const columns: number[] = [];
  await page.route(
    "**/api/v1/tasks/log-fixture/artifacts/log-artifact/lines?*",
    (route) => {
      const column = Number(
        new URL(route.request().url()).searchParams.get("start_column"),
      );
      columns.push(column);
      return route.fulfill({
        json: {
          request_id: "fixture",
          data: {
            artifact_id: "log-artifact",
            sha256: "b".repeat(64),
            total_lines: 1,
            lines: [
              {
                number: 1,
                content: column ? "end-of-line\n" : "中文".repeat(2000),
                column,
                complete: column > 0,
              },
            ],
            next_line: column ? null : 1,
            next_column: column ? null : 4000,
          },
        },
      });
    },
  );
  await open(page, false);
  await expect(page.getByText(/本行未完，下一页继续/)).toBeVisible();
  await page.getByRole("button", { name: "下一页日志" }).click();
  await expect(page.getByLabel("分页日志内容")).toContainText("end-of-line");
  await expect(page.getByLabel("分页日志内容")).toContainText("1 续");
  await expect(page.getByRole("button", { name: "下一页日志" })).toBeDisabled();
  expect(columns).toEqual([0, 4000]);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("failed page reads retry the same cursor and keep task evidence available", async ({
  page,
}) => {
  let failed = false;
  await page.route(
    "**/api/v1/tasks/log-fixture/artifacts/log-artifact/lines?*",
    (route) => {
      const line = Number(
        new URL(route.request().url()).searchParams.get("start_line"),
      );
      if (line === 216 && !failed) {
        failed = true;
        return route.fulfill({
          status: 503,
          json: {
            request_id: "fixture",
            error: { code: "temporary", message: "日志暂时不可读" },
          },
        });
      }
      return route.fulfill({
        json: { request_id: "fixture", data: window(line) },
      });
    },
  );
  await open(page);
  await expect(page.getByText(/L116–L215/)).toBeVisible();
  await page.getByRole("button", { name: "下一页日志" }).click();
  await expect(page.getByRole("alert")).toContainText("日志暂时不可读");
  await expect(
    page.getByRole("heading", { name: "长日志测试任务" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "重试读取" }).click();
  await expect(page.getByText(/L216–L315/)).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
});
