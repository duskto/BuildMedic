import { test, expect } from "@playwright/test";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

test("scoring: real detached receipts match the page without rewriting public results", async ({
  page,
  context,
  browser,
  baseURL,
}) => {
  const root = resolve(process.cwd(), "../../var/acceptance");
  const path = resolve(root, "four-arm-session.json");
  test.skip(
    process.env.BUILDMEDIC_LIVE !== "1" || !existsSync(path),
    "Requires an already completed and independently scored experiment; creates no model tasks.",
  );
  const state = JSON.parse(await readFile(path, "utf8"));
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
  const url = `/api/v1/experiments/${state.experiment_id}`;
  const original = (await (await page.request.get(url)).json()).data;
  const score = (await (await page.request.get(url + "/score")).json()).data;
  expect(score.status).toBe("completed");
  expect(score.report_sha256).toBe(original.report_sha256);
  expect(
    score.summary.every((s: any) => s.evaluation_errors === 0),
  ).toBeTruthy();
  const repeat = (await (await page.request.post(url + "/score")).json()).data;
  expect(repeat.id).toBe(score.id);
  expect(repeat.receipt.finished_at).toBe(score.receipt.finished_at);
  await page.goto(`/experiments/${state.experiment_id}`);
  await expect(
    page.getByRole("heading", { name: "独立行为评测" }),
  ).toBeVisible();
  await expect(page.getByText("评分已完成", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("link", { name: "下载独立评分回执" }),
  ).toHaveAttribute("href", url + "/score/report");
  const table = page
    .getByRole("heading", { name: "独立行为评测" })
    .locator("..")
    .getByRole("table");
  for (const s of score.summary.filter((s: any) => s.repair_applicable)) {
    const names: Record<string, string> = {
      standard: "单次固定策略",
      multi: "独立多次策略",
      adaptive: "Harness²",
    };
    const row = table.getByRole("row").filter({
      has: page.getByRole("cell", { name: names[s.mode], exact: true }),
    });
    await expect(row).toContainText(`${s.behavior_verified} / ${s.planned}`);
    await expect(row).toContainText(
      `${s.false_positives} / ${s.false_positive_denominator}`,
    );
  }
  expect((await (await page.request.get(url)).json()).data).toEqual(original);
  const response = await page.request.get(url + "/score/report");
  expect((await response.json()).id).toBe(score.id);
  await page.screenshot({
    path: resolve(root, "scoring.png"),
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
    path: resolve(root, "scoring-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
  const other = await browser.newContext();
  expect((await other.request.get(`${baseURL}${url}/score`)).status()).toBe(
    404,
  );
  expect((await other.request.post(`${baseURL}${url}/score`)).status()).toBe(
    404,
  );
  await other.close();
  expect(errors).toEqual([]);
  console.log(
    JSON.stringify({
      score: score.id,
      report: score.report_sha256,
      summary: score.summary,
    }),
  );
});
