import { test, expect } from "@playwright/test";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

test("logs: existing real artifacts remain private and readable through bounded pages", async ({
  page,
  context,
  browser,
  baseURL,
}) => {
  const root = resolve(process.cwd(), "../../var/acceptance");
  const statePath = resolve(root, "four-arm-session.json");
  test.skip(
    process.env.BUILDMEDIC_LIVE !== "1" || !existsSync(statePath),
    "Reads a previously completed comparison; creates no model tasks.",
  );
  const state = JSON.parse(await readFile(statePath, "utf8"));
  await context.addCookies([
    {
      name: "buildmedic_session",
      value: state.cookie,
      url: new URL(baseURL!).origin,
      httpOnly: true,
      sameSite: "Strict",
    },
  ]);
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const comparison = (
    await (
      await page.request.get(`/api/v1/experiments/${state.experiment_id}`)
    ).json()
  ).data;
  const tid = comparison.rows.find((r: any) => r.mode === "standard").task_id;
  const before = (await (await page.request.get(`/api/v1/tasks/${tid}`)).json())
    .data;
  const evidence = before.evidence.find((e: any) => e.kind === "baseline_log");
  const path = `/api/v1/tasks/${tid}/artifacts/${evidence.artifact_id}`;
  const original = await (await page.request.get(path)).text();
  const first = await page.request.get(`${path}/lines`);
  expect(first.status()).toBe(200);
  const window = (await first.json()).data;
  expect(window.sha256).toBe(
    before.artifacts.find((a: any) => a.id === evidence.artifact_id).sha256,
  );
  expect(window.lines.length).toBeLessThanOrEqual(100);
  let read = window.lines.map((l: any) => l.content).join("");
  let cursor = window;
  let pages = 1;
  while (cursor.next_line !== null) {
    expect(pages++).toBeLessThan(100);
    cursor = (
      await (
        await page.request.get(
          `${path}/lines?start_line=${cursor.next_line}&start_column=${cursor.next_column}`,
        )
      ).json()
    ).data;
    read += cursor.lines.map((l: any) => l.content).join("");
  }
  expect(read).toBe(original);
  await page.goto(`/tasks/${tid}?tab=evidence`);
  const card = page.locator(`article[id="${evidence.id}"]`);
  await card.getByRole("button", { name: "展开完整上下文" }).click();
  await expect(card.getByLabel("分页日志内容")).toBeVisible();
  await expect(
    card.getByRole("link", { name: "下载完整脱敏日志" }),
  ).toHaveAttribute("href", `${path}?download=true`);
  await page.screenshot({
    path: resolve(root, "logs.png"),
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
    path: resolve(root, "logs-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
  const other = await browser.newContext();
  expect((await other.request.get(`${baseURL}${path}/lines`)).status()).toBe(
    404,
  );
  await other.close();
  const after = (await (await page.request.get(`/api/v1/tasks/${tid}`)).json())
    .data;
  expect(after.usage).toEqual(before.usage);
  expect(after.last_event_seq).toBe(before.last_event_seq);
  expect(errors).toEqual([]);
});
