import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";

test("grouped registration preserves the complete frozen test partition and does not start tasks", async ({
  page,
}) => {
  const testPlan = JSON.parse(
    readFileSync(
      new URL("../../../../evaluation/grouped-test.json", import.meta.url),
      "utf8",
    ),
  );
  const developmentPlan = JSON.parse(
    readFileSync(
      new URL(
        "../../../../evaluation/grouped-development.json",
        import.meta.url,
      ),
      "utf8",
    ),
  );
  let created: Record<string, unknown> | null = null;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown;
    if (path.endsWith("/health"))
      data = {
        model_ready: true,
        model_id: "explicit-fixture",
        adaptive_ready: true,
        adaptive_message: "fixture",
        runner: { ready: true, message: "fixture", backend: "docker" },
      };
    else if (path.endsWith("/evaluation/cases"))
      data = developmentPlan.cases.slice(0, 3);
    else if (path.endsWith("/evaluation/grouped-plans"))
      data = { development: developmentPlan, test: testPlan };
    else if (
      path.endsWith("/experiments") &&
      route.request().method() === "POST"
    ) {
      created = route.request().postDataJSON();
      expect(created?.schema_version).toBe("3.0");
      expect(created?.dataset_sha256).toBe(testPlan.dataset_sha256);
      expect(created?.dataset_manifest).toEqual(testPlan.dataset_manifest);
      expect(created?.cases).toEqual(testPlan.cases);
      expect(created?.partition).toBe("test");
      data = {
        ...report,
        id: "grouped-fixture",
        manifest: created,
        finished: 0,
        planned: 40,
        rows: [],
        status: "planned",
      };
    } else if (path.endsWith("/experiments"))
      data = { items: [], total: 0, page: 1, page_size: 10 };
    else if (path.endsWith("/experiments/grouped-fixture"))
      data = {
        ...report,
        id: "grouped-fixture",
        manifest: created,
        finished: 0,
        planned: 40,
        rows: [],
        status: "planned",
      };
    else if (path.endsWith("/analysis"))
      data = {
        source_report_sha256: report.report_sha256,
        complete: false,
        environment_consistent: null,
        repeat_results: [],
        variation: [],
        pairs: [],
      };
    else
      throw new Error(
        `Unexpected grouped fixture request: ${route.request().method()} ${path}`,
      );
    await route.fulfill({
      json: { data, request_id: "explicit-grouped-fixture" },
    });
  });
  await page.goto("/experiments");
  await page
    .getByRole("combobox", { name: "实验组别", exact: true })
    .selectOption("3.0-test");
  await expect(
    page.getByText("共 40 项执行。", { exact: false }),
  ).toBeVisible();
  await page.getByRole("button", { name: "登记对照清单", exact: true }).click();
  await expect(page).toHaveURL(/experiments\/grouped-fixture$/);
  await expect(
    page.getByText("公开构造分区：测试", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText("分组构造任务尚无独立私有行为评分包。", { exact: false }),
  ).toBeVisible();
  expect(created).not.toBeNull();
});

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({
      json: {
        request_id: "fixture",
        data: {
          model_ready: false,
          model_id: null,
          adaptive_ready: false,
          adaptive_message: "fixture",
          scoring_ready: false,
          runner: { ready: false, message: "fixture", backend: null },
        },
      },
    }),
  );
});

const report = {
  id: "comparison-fixture",
  created_at: 1,
  status: "completed",
  finished: 2,
  planned: 2,
  manifest: {
    name: "development-fixture",
    dataset: "public-development",
    repeats: 1,
    agent_tools: false,
    cases: [{ case_id: "pagination", snapshot_sha256: "a".repeat(64) }],
    limits: { max_model_calls: 12, max_tokens: 100000, max_wall_seconds: 600 },
  },
  manifest_sha256: "b".repeat(64),
  configuration_sha256: "c".repeat(64),
  report_sha256: "d".repeat(64),
  configuration: {
    provider: "fixture",
    model_id: "scripted-explicit-fixture",
    harness2_sha: "1".repeat(40),
  },
  scope: "公开开发集与公开回归检查；未评测隐藏行为或泛化收益",
  environment_consistent: true,
  summary: ["standard", "adaptive"].map((mode, i) => ({
    mode,
    planned: 1,
    finished: 1,
    public_verified: 1 - i,
    reproduced: 1,
    calls: 1 + i * 6,
    tokens: 1000 + i * 6000,
    unknown_usage: i,
    cost_usd: null,
    cost_per_public_verified: null,
    tokens_per_public_verified: i ? null : 1000,
    wall_seconds: 10 + i * 50,
    checks: 6 + i * 6,
    rollouts: 1 + i * 3,
    blocked: 0,
    budget_exhausted: i,
    interrupted: 0,
  })),
  rows: ["standard", "adaptive"].map((mode, i) => ({
    mode,
    repeat: 1,
    case_id: "pagination",
    task_id: `task-${i}`,
    metrics: {
      public_verified: i === 0,
      lifecycle: "completed",
      outcome: i ? "budget_exhausted" : "repaired",
      calls: 1 + i * 6,
      tokens: 1000 + i * 6000,
      usage_known: i === 0,
      wall_seconds: 10 + i * 50,
      checks: 6 + i * 6,
    },
  })),
};

const emptyAnalysis = {
  source_report_sha256: report.report_sha256,
  complete: true,
  environment_consistent: true,
  repeat_results: [],
  variation: [],
  pairs: [],
};

for (const partial of [false, true]) {
  test(`descriptive analysis keeps ${partial ? "pending pairs" : "unknown usage and single-repeat variance"} explicit`, async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const stats = {
      n: 1,
      mean: 1,
      minimum: 1,
      maximum: 1,
      sample_stddev: null,
    };
    const analysis = {
      ...emptyAnalysis,
      complete: !partial,
      repeat_results: [
        {
          mode: "standard",
          repeat: 1,
          planned: 1,
          finished: 1,
          public_verified: 1,
          public_rate: 1,
          calls: 1,
          tokens: 1000,
          wall_seconds: 10,
        },
      ],
      variation: [
        {
          mode: "standard",
          planned_repeats: 1,
          complete_repeats: 1,
          public_rate: stats,
          tokens: { ...stats, mean: 1000 },
        },
      ],
      pairs: [
        {
          reference: "standard",
          compared: "adaptive",
          planned: 1,
          paired: partial ? 0 : 1,
          pending: partial ? 1 : 0,
          compared_only: 0,
          reference_only: partial ? 0 : 1,
          both_verified: 0,
          neither_verified: 0,
          public_rate_delta: partial ? null : -1,
          calls_delta: 6,
          tokens_delta: null,
          unknown_usage_pairs: 1,
          wall_seconds_delta: 50,
          cost_usd_delta: null,
          unknown_cost_pairs: 1,
        },
      ],
    };
    await page.route("**/api/v1/experiments/**", (route) =>
      route.fulfill({
        json: {
          request_id: "fixture",
          data: route.request().url().endsWith("/analysis")
            ? analysis
            : route.request().url().endsWith("/score")
              ? null
              : report,
        },
      }),
    );
    await page.goto("/experiments/comparison-fixture");
    const panel = page.locator("section").filter({
      has: page.getByRole("heading", { name: "重复运行与配对比较" }),
    });
    await expect(panel.getByText(/至少需两轮完整记录/)).toBeVisible();
    await expect(
      panel.getByRole("cell", { name: "固定策略 → Harness²", exact: true }),
    ).toBeVisible();
    await expect(
      panel.getByRole("cell", {
        name: partial ? "待完整配对" : "-100.0 个百分点",
        exact: true,
      }),
    ).toBeVisible();
    if (partial)
      await expect(panel.getByText(/本批尚未完整结束/)).toBeVisible();
    else
      await expect(
        panel.getByRole("cell", { name: "+6 / 未知", exact: true }),
      ).toBeVisible();
    await expect(
      panel.getByRole("link", { name: "下载可复算分析" }),
    ).toHaveAttribute(
      "href",
      "/api/v1/experiments/comparison-fixture/analysis/report",
    );
    await expect(panel.getByRole("alert")).toHaveCount(0);
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(
      panel.getByRole("columnheader", { name: "公开通过", exact: true }),
    ).toBeVisible();
    const scroll = panel.getByRole("region", {
      name: "同案例配对表格，可横向滚动",
    });
    expect(await scroll.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(
      true,
    );
    await scroll.scrollIntoViewIfNeeded();
    await scroll.evaluate((el) => {
      el.scrollLeft = el.scrollWidth;
    });
    await expect(
      panel.getByRole("columnheader", { name: "耗时 / 费用差", exact: true }),
    ).toBeInViewport();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    expect(errors).toEqual([]);
  });
}

test("analysis from a different report snapshot waits instead of showing stale numbers", async ({
  page,
}) => {
  await page.route("**/api/v1/experiments/**", (route) =>
    route.fulfill({
      json: {
        request_id: "fixture",
        data: route.request().url().endsWith("/analysis")
          ? { ...emptyAnalysis, source_report_sha256: "e".repeat(64) }
          : route.request().url().endsWith("/score")
            ? null
            : report,
      },
    }),
  );
  await page.goto("/experiments/comparison-fixture");
  await expect(
    page.getByText(/报告正在更新，等待下一次刷新后核对分析/),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "同案例配对" })).toHaveCount(
    0,
  );
});

test("comparison shows actual counts, missing usage and task evidence without hidden-score claims", async ({
  page,
}) => {
  await page.route("**/api/v1/experiments/**", (route) =>
    route.fulfill({
      json: {
        data: route.request().url().endsWith("/score")
          ? null
          : route.request().url().endsWith("/analysis")
            ? emptyAnalysis
            : report,
        request_id: "fixture",
      },
    }),
  );
  await page.goto("/experiments/comparison-fixture");
  await expect(
    page.getByRole("heading", { name: "development-fixture" }),
  ).toBeVisible();
  await expect(page.getByText("已完成 · 已归档 2 / 2 项")).toBeVisible();
  await expect(page.getByText(/未评测隐藏行为或泛化收益/)).toBeVisible();
  await expect(page.getByText("1 项用量不完整")).toBeVisible();
  await expect(page.getByRole("link", { name: "查看任务" })).toHaveCount(2);
  await expect(
    page.getByRole("link", { name: "下载可复算报告" }),
  ).toHaveAttribute("href", "/api/v1/experiments/comparison-fixture/report");
  await expect(
    page.getByRole("button", { name: "运行 / 继续对照" }),
  ).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("registering a comparison pins source hashes and equal budgets before any execution", async ({
  page,
}) => {
  let captured: Record<string, unknown> | undefined;
  let advanced = 0;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/score"))
      return route.fulfill({ json: { data: null, request_id: "fixture" } });
    if (path.endsWith("/analysis"))
      return route.fulfill({
        json: {
          data: { ...emptyAnalysis, complete: false },
          request_id: "fixture",
        },
      });
    if (path.endsWith("/evaluation/cases"))
      return route.fulfill({
        json: {
          data: ["pagination", "imports", "signature"].map((id) => ({
            case_id: id,
            log_sha256: "e".repeat(64),
            title: id,
            description: id,
            category: "fixture",
            repo: "local/demo",
            snapshot_sha256: "a".repeat(64),
            head_sha: null,
            profile_version: "fixture",
            execution_label: "fixture",
          })),
          request_id: "fixture",
        },
      });
    if (path.endsWith("/health"))
      return route.fulfill({
        json: {
          data: {
            model_ready: true,
            model_id: "fixture",
            adaptive_ready: true,
            adaptive_message: "fixture",
            runner: { ready: true, message: "fixture", backend: "docker" },
          },
          request_id: "fixture",
        },
      });
    if (path.endsWith("/advance")) {
      advanced++;
      return route.fulfill({
        json: {
          data: { status: "completed", task_id: null },
          request_id: "fixture",
        },
      });
    }
    if (path === "/api/v1/experiments" && route.request().method() === "POST") {
      captured = route.request().postDataJSON();
      expect(route.request().headers()["idempotency-key"]).toBeTruthy();
      return route.fulfill({
        json: { data: { ...report, status: "planned" }, request_id: "fixture" },
      });
    }
    if (path === "/api/v1/experiments")
      return route.fulfill({
        json: {
          data: { items: [], total: 0, page: 1, page_size: 10 },
          request_id: "fixture",
        },
      });
    if (path.startsWith("/api/v1/experiments/"))
      return route.fulfill({
        json: { data: { ...report, status: "planned" }, request_id: "fixture" },
      });
    return route.fulfill({
      status: 404,
      json: {
        error: { code: "fixture_missing", message: "fixture" },
        request_id: "fixture",
      },
    });
  });
  await page.goto("/experiments");
  await page.getByRole("button", { name: "登记对照清单" }).click();
  await expect(
    page.getByRole("heading", { name: "development-fixture" }),
  ).toBeVisible();
  expect(captured?.limits).toEqual({
    max_model_calls: 12,
    max_tokens: 100000,
    max_wall_seconds: 600,
  });
  expect(captured?.cases).toHaveLength(3);
  expect(captured?.schema_version).toBe("2.0");
  expect(captured?.multi_attempts).toBe(4);
  expect((captured?.cases as { log_sha256: string }[])[0].log_sha256).toBe(
    "e".repeat(64),
  );
  expect(advanced).toBe(0);
  await page.getByRole("button", { name: "运行 / 继续对照" }).click();
  await expect.poll(() => advanced).toBe(1);
});

test("four-arm view separates unscored log diagnoses from verified repairs", async ({
  page,
}) => {
  const four = {
    ...report,
    manifest: { ...report.manifest, schema_version: "2.0", multi_attempts: 4 },
    finished: 4,
    planned: 4,
    summary: [
      {
        ...report.summary[0],
        mode: "logs",
        public_verified: 0,
        diagnoses: 1,
        diagnosis_accuracy: null,
        repair_applicable: false,
        calls: 1,
        tokens: 100,
        checks: 0,
      },
      { ...report.summary[0], repair_applicable: true },
      { ...report.summary[0], mode: "multi", repair_applicable: true },
      report.summary[1],
    ],
    rows: [
      {
        ...report.rows[0],
        mode: "logs",
        metrics: {
          ...report.rows[0].metrics,
          outcome: "diagnosed_only",
          public_verified: false,
          checks: 0,
        },
      },
      report.rows[0],
      { ...report.rows[0], mode: "multi" },
      report.rows[1],
    ],
  };
  await page.route("**/api/v1/experiments/**", (route) =>
    route.fulfill({
      json: {
        data: route.request().url().endsWith("/score")
          ? null
          : route.request().url().endsWith("/analysis")
            ? emptyAnalysis
            : four,
        request_id: "fixture",
      },
    }),
  );
  await page.goto("/experiments/comparison-fixture");
  await expect(page.getByText("不适用；交付 1 份诊断")).toBeVisible();
  await expect(page.getByText(/日志组诊断准确率未评分/)).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "固定策略 · 独立多次", exact: true }),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
