import { test, expect } from "@playwright/test";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

// Explicit opt-in: these checks contact the configured provider and incur model usage.
test.skip(
  process.env.BUILDMEDIC_LIVE !== "1",
  "Set BUILDMEDIC_LIVE=1 to run real provider acceptance.",
);
const output = resolve(process.cwd(), "../../var/acceptance");

for (const [caseId, title] of [
  ["pagination", "分页边界错误"],
  ["imports", "内部导入错误"],
  ["signature", "调用签名不一致"],
]) {
  test(`${caseId}: browser → real model → Docker → independent verification`, async ({
    page,
    context,
  }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "让失败的构建，有据可解。" }),
    ).toBeVisible();
    await page.getByRole("link", { name: "新建诊断", exact: true }).click();
    await page.getByRole("radio", { name: new RegExp(title) }).check();
    const created = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/v1/tasks") &&
        response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "开始诊断" }).click();
    const response = await created;
    expect(response.status()).toBe(202);
    const tid: string = (await response.json()).data.task_id;
    await expect(page).toHaveURL(new RegExp(`/tasks/${tid}$`));

    // A response lost by the browser must not enqueue a second paid diagnosis.
    const duplicate = await page.request.post("/api/v1/tasks", {
      headers: {
        "Idempotency-Key": response.request().headers()["idempotency-key"],
      },
      data: response.request().postDataJSON(),
    });
    expect((await duplicate.json()).data.task_id).toBe(tid);
    await page.reload();
    await expect(
      page.getByRole("heading", { name: title, exact: true }),
    ).toBeVisible();
    if (caseId === "pagination") {
      await context.setOffline(true);
      await page.waitForTimeout(1800);
      await context.setOffline(false);
    }

    let task: any;
    await expect
      .poll(
        async () => {
          const result = await page.request.get(`/api/v1/tasks/${tid}`);
          task = (await result.json()).data;
          return task.lifecycle;
        },
        { timeout: 150_000, intervals: [500, 1000, 2000] },
      )
      .toMatch(/^(completed|failed|interrupted|cancelled)$/);
    await mkdir(output, { recursive: true });
    await writeFile(
      resolve(output, `${caseId}.json`),
      JSON.stringify(task, null, 2),
    );
    expect(task.outcome, task.message).toBe("repaired");
    expect(task.reproduction_status).toBe("reproduced");
    expect(task.validation_status).toBe("passed");
    expect(task.model_calls.length).toBeGreaterThan(0);
    expect(
      task.model_calls.some((call: any) => call.status === "completed"),
    ).toBeTruthy();
    expect(
      task.executions.every(
        (execution: any) => execution.isolation === "docker",
      ),
    ).toBeTruthy();
    const independent = task.validations.find(
      (value: any) => value.kind === "independent",
    );
    expect(independent.status).toBe("passed");
    expect(independent.target.counts.passed).toBeGreaterThan(0);
    expect(independent.regression.counts.passed).toBeGreaterThan(0);
    expect(independent.regression.image_digest).toMatch(
      /^sha256:[a-f0-9]{64}$/,
    );
    await expect(
      page.getByRole("heading", { name: "本地验证通过", exact: true }),
    ).toBeVisible();
    await page.screenshot({
      path: resolve(output, `${caseId}-overview.png`),
      fullPage: true,
    });

    await page.locator(".evidence-chips button").first().click();
    await expect(page.getByRole("tab", { name: "证据与日志" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    const evidence = page.locator(".evidence-card.highlighted");
    await expect(evidence).toBeVisible();
    await evidence.getByRole("button", { name: "展开完整上下文" }).click();
    await expect(evidence.locator(".numbered-log")).toBeVisible();

    await page.getByRole("tab", { name: /修复方案/ }).click();
    await expect(page.locator(".diff-lines")).toBeVisible();
    const patchDownloaded = page.waitForEvent("download");
    await page.getByRole("link", { name: "下载 patch", exact: true }).click();
    const patchPath = await (await patchDownloaded).path();
    expect(patchPath).not.toBeNull();
    const patch = await readFile(patchPath!);
    const candidate = task.candidates.find((value: any) => value.selected);
    expect(createHash("sha256").update(patch).digest("hex")).toBe(
      candidate.patch_hash,
    );
    await page.screenshot({
      path: resolve(output, `${caseId}-patch.png`),
      fullPage: true,
    });
    await page.getByRole("tab", { name: "验证结果" }).click();
    await expect(
      page.getByRole("heading", { name: /最终独立验证/ }),
    ).toBeVisible();

    if (caseId === "pagination") {
      await page.getByRole("tab", { name: /修复方案/ }).click();
      await page
        .getByRole("button", { name: "重新独立验证", exact: true })
        .click();
      await expect
        .poll(
          async () => {
            task = (
              await (await page.request.get(`/api/v1/tasks/${tid}`)).json()
            ).data;
            return task.validations.find(
              (value: any) => value.kind === "revalidation",
            )?.status;
          },
          { timeout: 60_000 },
        )
        .toBe("passed");
      expect(
        task.validations.filter((value: any) => value.kind === "independent"),
      ).toHaveLength(1);
      await page.reload();
      await page.getByRole("tab", { name: "验证结果" }).click();
      await expect(
        page.getByRole("heading", { name: /重新验证/ }),
      ).toBeVisible();
      await page.setViewportSize({ width: 390, height: 844 });
      await expect
        .poll(() =>
          page
            .locator(".sidebar")
            .evaluate((element) => element.getBoundingClientRect().right),
        )
        .toBeLessThanOrEqual(0);
      await page.screenshot({
        path: resolve(output, "pagination-mobile.png"),
        fullPage: true,
        animations: "disabled",
      });
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      ).toBeTruthy();
    }
    const reportDownloaded = page.waitForEvent("download");
    await page.getByRole("link", { name: "下载报告", exact: true }).click();
    const report = JSON.parse(
      await readFile((await (await reportDownloaded).path()) as string, "utf8"),
    );
    expect(report.outcome).toBe("repaired");
    expect(report.model_id).toBe(task.model_calls[0].model);
    await writeFile(
      resolve(output, `${caseId}.json`),
      JSON.stringify(task, null, 2),
    );
    await context.storageState({
      path: resolve(output, `${caseId}-session.json`),
    });
    expect(errors).toEqual([]);
    console.log(
      JSON.stringify({
        case_id: caseId,
        task_id: tid,
        outcome: task.outcome,
        model_calls: task.usage.calls,
        tokens: task.usage.tokens,
        patch_hash: candidate.patch_hash,
        image_digest: independent.regression.image_digest,
      }),
    );
  });
}

test("blocked environment has evidence and can be retried without losing history", async ({
  page,
}) => {
  await page.goto("/tasks/new?case=missing-dependency");
  await page.getByRole("button", { name: "开始诊断" }).click();
  await expect(
    page.getByRole("heading", { name: "环境阻塞", exact: true }),
  ).toBeVisible();
  const original = page.url().split("/").pop();
  await page.getByRole("button", { name: "重新诊断", exact: true }).click();
  await expect(page).not.toHaveURL(new RegExp(`/tasks/${original}$`));
  await expect(
    page.getByRole("heading", { name: "环境阻塞", exact: true }),
  ).toBeVisible();
  const current = page.url().split("/").pop();
  const snapshot = (
    await (await page.request.get(`/api/v1/tasks/${current}`)).json()
  ).data;
  expect(snapshot.parent_task_id).toBe(original);
  expect(snapshot.evidence.length).toBeGreaterThan(0);
  expect(snapshot.usage.calls).toBe(0);
});
