import { test, expect } from "@playwright/test";

test("private behavior failures are separate from public results and scoring creates no model tasks", async ({
  page,
}) => {
  let started = 0;
  const summary = {
    mode: "standard",
    planned: 1,
    finished: 1,
    public_verified: 1,
    reproduced: 1,
    calls: 1,
    tokens: 1000,
    unknown_usage: 0,
    cost_usd: null,
    cost_per_public_verified: null,
    tokens_per_public_verified: 1000,
    wall_seconds: 3,
    checks: 6,
    rollouts: 1,
    blocked: 0,
    budget_exhausted: 0,
    interrupted: 0,
  };
  const experiment = {
    id: "owned-experiment",
    created_at: 1,
    status: "completed",
    finished: 1,
    planned: 1,
    manifest: {
      name: "independent-fixture",
      dataset: "public-development",
      schema_version: "2.0",
      multi_attempts: 4,
      repeats: 1,
      agent_tools: false,
      cases: [{ case_id: "pagination", snapshot_sha256: "a".repeat(64) }],
      limits: {
        max_model_calls: 12,
        max_tokens: 100000,
        max_wall_seconds: 600,
      },
    },
    configuration: {
      model_id: "explicit-fixture",
      provider: "fixture",
      harness2_sha: "1".repeat(40),
    },
    manifest_sha256: "a".repeat(64),
    configuration_sha256: "b".repeat(64),
    report_sha256: "c".repeat(64),
    environment_consistent: true,
    scope: "公开开发集结果",
    summary: [summary],
    rows: [
      {
        case_id: "pagination",
        repeat: 1,
        mode: "standard",
        task_id: "owned-task",
        metrics: {
          lifecycle: "completed",
          outcome: "repaired",
          public_verified: true,
          calls: 1,
          tokens: 1000,
          usage_known: true,
          wall_seconds: 3,
          checks: 6,
        },
      },
    ],
  };
  const score = {
    id: "owned-score",
    experiment_id: experiment.id,
    status: "completed",
    created_at: 2,
    report_sha256: experiment.report_sha256,
    suite_sha256: "d".repeat(64),
    error_code: null,
    scope: "开发案例独立行为检查",
    receipt: {
      image_digest: "sha256:" + "e".repeat(64),
      status: "completed",
      signature: "f".repeat(64),
    },
    summary: [
      {
        mode: "standard",
        repair_applicable: true,
        planned: 1,
        behavior_verified: 0,
        evaluated: 1,
        evaluation_errors: 0,
        not_reproduced: 0,
        no_candidate: 0,
        false_positives: 1,
        false_positive_denominator: 1,
        new_regressions: 1,
        regression_denominator: 1,
      },
    ],
  };
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown;
    if (path.endsWith("/health"))
      data = {
        model_ready: true,
        model_id: "fixture",
        adaptive_ready: true,
        adaptive_message: "fixture",
        runner: { ready: true, message: "fixture", backend: "docker" },
        scoring_ready: true,
      };
    else if (path.endsWith("/score")) {
      if (route.request().method() === "POST") {
        started++;
        expect(route.request().postData()).toBeNull();
        data = score;
      } else data = null;
    } else if (path.endsWith("/analysis"))
      data = {
        source_report_sha256: experiment.report_sha256,
        complete: true,
        environment_consistent: true,
        repeat_results: [],
        variation: [],
        pairs: [],
      };
    else if (path === "/api/v1/experiments/owned-experiment") data = experiment;
    else {
      await route.fulfill({
        status: 404,
        json: {
          error: { message: "No fixture", code: "fixture_missing" },
          request_id: "fixture",
        },
      });
      return;
    }
    await route.fulfill({ json: { data, request_id: "fixture" } });
  });
  await page.goto("/experiments/owned-experiment");
  await expect(
    page.getByRole("heading", { name: "independent-fixture" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "运行独立行为评测" }).click();
  await expect(page.getByText("评分已完成", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "0 / 1", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "1 / 1", exact: true }),
  ).toHaveCount(4);
  await expect(
    page.getByRole("link", { name: "下载独立评分回执" }),
  ).toHaveAttribute(
    "href",
    "/api/v1/experiments/owned-experiment/score/report",
  );
  expect(started).toBe(1);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
