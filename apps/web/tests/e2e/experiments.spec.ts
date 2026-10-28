import { test, expect } from "@playwright/test";

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

test("comparison shows actual counts, missing usage and task evidence without hidden-score claims", async ({
  page,
}) => {
  await page.route("**/api/v1/experiments/**", (route) =>
    route.fulfill({ json: { data: report, request_id: "fixture" } }),
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
    if (path.endsWith("/demo-cases"))
      return route.fulfill({
        json: {
          data: ["pagination", "imports", "signature"].map((id) => ({
            id,
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
  expect(advanced).toBe(0);
  await page.getByRole("button", { name: "运行 / 继续对照" }).click();
  await expect.poll(() => advanced).toBe(1);
});
