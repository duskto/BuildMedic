import { test, expect } from "@playwright/test";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

test("comparison: persisted real API results match the browser and remain private", async ({
  page,
  context,
  browser,
  baseURL,
}) => {
  const root = resolve(process.cwd(), "../../var/acceptance");
  const prefix = process.env.BUILDMEDIC_COMPARISON_PREFIX ?? "evaluation";
  const sessionFile = resolve(
    root,
    process.env.BUILDMEDIC_COMPARISON_STATE ?? "evaluation-session.json",
  );
  test.skip(
    process.env.BUILDMEDIC_LIVE !== "1" || !existsSync(sessionFile),
    "Requires an existing CLI comparison session; creates no paid tasks.",
  );
  test.setTimeout(360_000);
  const state = JSON.parse(await readFile(sessionFile, "utf8"));
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await context.addCookies([
    {
      name: "buildmedic_session",
      value: state.cookie,
      url: new URL(baseURL!).origin,
      httpOnly: true,
      sameSite: "Strict",
    },
  ]);
  let report: any;
  await expect
    .poll(
      async () => {
        const response = await page.request.get(
          `/api/v1/experiments/${state.experiment_id}`,
        );
        expect(response.status()).toBe(200);
        report = (await response.json()).data;
        return report.status;
      },
      { timeout: 300_000, intervals: [2000] },
    )
    .toBe("completed");
  await page.goto(`/experiments/${state.experiment_id}`);
  await expect(
    page.getByRole("heading", { name: report.manifest.name }),
  ).toBeVisible();
  await expect(
    page.getByText(`已完成 · 已归档 ${report.finished} / ${report.planned} 项`),
  ).toBeVisible();
  for (const summary of report.summary) {
    const row = page.getByRole("row").filter({
      has: page.getByRole("cell", {
        name: new RegExp(
          (
            {
              logs: "^日志解释.*已归档",
              standard: "^固定策略 · 已归档",
              multi: "^固定策略 · 独立多次.*已归档",
              adaptive: "^Harness².*已归档",
            } as Record<string, string>
          )[summary.mode],
        ),
      }),
    });
    await expect(row).toContainText(
      summary.repair_applicable === false
        ? `不适用；交付 ${summary.diagnoses} 份诊断`
        : `${summary.public_verified} / ${summary.planned}`,
    );
    await expect(row).toContainText(summary.tokens.toLocaleString("en-US"));
  }
  await expect(page.getByRole("link", { name: "查看任务" })).toHaveCount(
    report.planned,
  );
  expect(
    (
      await (
        await page.request.get(`/api/v1/experiments/${report.id}/report`)
      ).json()
    ).report_sha256,
  ).toBe(report.report_sha256);
  const analysis = (
    await (
      await page.request.get(`/api/v1/experiments/${report.id}/analysis`)
    ).json()
  ).data;
  expect(analysis.source_report_sha256).toBe(report.report_sha256);
  expect(analysis.complete).toBe(true);
  await expect(page.getByRole("heading", { name: "同案例配对" })).toBeVisible();
  await expect(page.getByText(/至少需两轮完整记录/)).toHaveCount(
    analysis.variation.filter((v: any) => v.public_rate.n < 2).length,
  );
  const analysisExport = await page.request.get(
    `/api/v1/experiments/${report.id}/analysis/report`,
  );
  expect(analysisExport.status()).toBe(200);
  expect((await analysisExport.json()).analysis).toEqual(analysis);
  await page.screenshot({
    path: resolve(root, `${prefix}.png`),
    fullPage: true,
    animations: "disabled",
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: resolve(root, `${prefix}-mobile.png`),
    fullPage: true,
    animations: "disabled",
  });
  const other = await browser.newContext();
  expect(
    (
      await other.request.get(`${baseURL}/api/v1/experiments/${report.id}`)
    ).status(),
  ).toBe(404);
  for (const suffix of ["/analysis", "/analysis/report"])
    expect(
      (
        await other.request.get(
          `${baseURL}/api/v1/experiments/${report.id}${suffix}`,
        )
      ).status(),
    ).toBe(404);
  await other.close();
  for (const row of report.rows.filter((r: any) => r.mode === "logs")) {
    const task = (
      await (await page.request.get(`/api/v1/tasks/${row.task_id}`)).json()
    ).data;
    expect(task.executions).toEqual([]);
    expect(task.evidence.every((e: any) => e.kind === "ci_log")).toBeTruthy();
  }
  expect(errors).toEqual([]);
  console.log(
    JSON.stringify({
      id: report.id,
      status: report.status,
      summary: report.summary,
    }),
  );
});
