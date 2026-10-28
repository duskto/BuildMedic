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
  const sessionFile = resolve(root, "evaluation-session.json");
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
    const row = page
      .getByRole("row")
      .filter({
        has: page.getByRole("cell", {
          name: new RegExp(
            summary.mode === "standard"
              ? "固定策略.*已归档"
              : "Harness².*已归档",
          ),
        }),
      });
    await expect(row).toContainText(
      `${summary.public_verified} / ${summary.planned}`,
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
  await page.screenshot({
    path: resolve(root, "evaluation.png"),
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
    path: resolve(root, "evaluation-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
  const other = await browser.newContext();
  expect(
    (
      await other.request.get(`${baseURL}/api/v1/experiments/${report.id}`)
    ).status(),
  ).toBe(404);
  await other.close();
  expect(errors).toEqual([]);
  console.log(
    JSON.stringify({
      id: report.id,
      status: report.status,
      summary: report.summary,
    }),
  );
});
