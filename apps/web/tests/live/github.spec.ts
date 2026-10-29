import { test, expect } from "@playwright/test";
import { chmod, mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createHash } from "node:crypto";

const runUrl = process.env.BUILDMEDIC_GITHUB_ACCEPTANCE_RUN_URL;
const expectedSha = process.env.BUILDMEDIC_GITHUB_ACCEPTANCE_SHA;
const expectedJob = Number(process.env.BUILDMEDIC_GITHUB_ACCEPTANCE_JOB_ID);
const root = resolve(process.cwd(), "../../var/acceptance");
const sessionFile = resolve(root, "github-live-session.json");
const taskFile = resolve(root, "github-live-task.json");

test.skip(
  process.env.BUILDMEDIC_LIVE !== "1" ||
    !runUrl ||
    !expectedSha ||
    !expectedJob,
  "Requires explicit real GitHub Run/SHA/Job and live-provider opt-in; incurs model usage.",
);

test("github: real failed Run → native Harness² → independent Docker verification", async ({
  page,
  context,
}) => {
  test.setTimeout(540_000);
  expect(expectedSha).toMatch(/^[a-f0-9]{40}$/);
  const url = new URL(runUrl!);
  expect(url.origin).toBe("https://github.com");
  const match = url.pathname.match(
    /^\/([^/]+\/[^/]+)\/actions\/runs\/([1-9][0-9]*)\/?$/,
  );
  expect(match).not.toBeNull();
  const repository = match![1];
  const runId = Number(match![2]);
  const attempt = Number(url.searchParams.get("attempt"));
  expect(attempt).toBeGreaterThan(0);
  expect(Number.isSafeInteger(expectedJob)).toBe(true);
  await mkdir(root, { recursive: true });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let id: string;

  if (process.env.BUILDMEDIC_GITHUB_ACCEPTANCE_RESUME === "1") {
    const session = JSON.parse(await readFile(sessionFile, "utf8"));
    await context.addCookies(session.cookies);
    const previous = JSON.parse(await readFile(taskFile, "utf8"));
    expect(previous.head_sha).toBe(expectedSha);
    expect(previous.run_id).toBe(runId);
    expect(previous.job_id).toBe(expectedJob);
    id = previous.id;
  } else {
    await page.goto("/tasks/new");
    await page
      .getByRole("button", { name: "GitHub Actions", exact: true })
      .click();
    await page.getByLabel("Actions Run URL").fill(runUrl!);
    const previewing = page.waitForResponse((r) =>
      r.url().endsWith("/api/v1/ci/preview"),
    );
    await page.getByRole("button", { name: "读取运行记录" }).click();
    const response = await previewing;
    expect(
      response.status(),
      "Real GitHub preview failed; check local server credentials",
    ).toBe(200);
    const preview = (await response.json()).data;
    expect(preview.head_sha).toBe(expectedSha);
    expect(preview.run_id).toBe(runId);
    expect(preview.run_attempt).toBe(attempt);
    expect(
      preview.jobs.find((j: any) => j.id === expectedJob)?.execution_supported,
    ).toBe(true);
    await page.getByRole("radio", { name: /pytest.*可独立复现/ }).check();
    await page.getByRole("radio", { name: /自适应诊断/ }).check();
    await expect(page.getByLabel("Token 上限")).toHaveValue("150000");
    const creating = page.waitForResponse(
      (r) =>
        r.url().endsWith("/api/v1/tasks") && r.request().method() === "POST",
    );
    await page.getByRole("button", { name: "开始诊断" }).click();
    const created = await creating;
    expect(created.status()).toBe(202);
    id = (await created.json()).data.task_id;
    const request = created.request();
    expect(request.postDataJSON().limits.max_tokens).toBe(150000);
    const duplicate = await page.request.post("/api/v1/tasks", {
      headers: { "Idempotency-Key": request.headers()["idempotency-key"] },
      data: request.postDataJSON(),
    });
    expect((await duplicate.json()).data.task_id).toBe(id);
    await context.storageState({ path: sessionFile });
    await chmod(sessionFile, 0o600);
    await writeFile(
      taskFile,
      JSON.stringify(
        { id, head_sha: expectedSha, run_id: runId, job_id: expectedJob },
        null,
        2,
      ),
    );
  }

  await page.goto(`/tasks/${id}`);
  let task: any;
  await expect
    .poll(
      async () => {
        const response = await page.request.get(`/api/v1/tasks/${id}`);
        expect(response.status()).toBe(200);
        task = (await response.json()).data;
        return task.lifecycle;
      },
      { timeout: 480_000, intervals: [1000, 2000, 3000] },
    )
    .toMatch(/^(completed|failed|cancelled|interrupted)$/);
  await writeFile(taskFile, JSON.stringify(task, null, 2));
  expect(task.outcome, task.message).toBe("repaired");
  expect(task.source_type).toBe("github_actions");
  expect(task.repo).toBe(repository);
  expect(task.head_sha).toBe(expectedSha);
  expect(task.run_attempt).toBe(attempt);
  expect(task.job_id).toBe(expectedJob);
  expect(task.source_snapshot.head_sha).toBe(expectedSha);
  expect(task.validation_status).toBe("passed");
  expect(task.reproduction_status).toBe("reproduced");
  expect(task.harness2_sha).toBe("1cfe254e8d25b15f37048c3730134686abe60335");
  expect(task.rollouts.map((r: any) => r.label).sort()).toEqual([
    "base1",
    "c1",
    "dom1",
    "inv1",
  ]);
  expect(task.harness.stages).toHaveLength(3);
  expect(task.harness.proposals.some((p: any) => p.changed)).toBe(true);
  expect(task.usage.calls).toBeGreaterThanOrEqual(7);
  expect(task.baseline[0].counts.failed).toBeGreaterThan(0);
  const ci = task.evidence.find((e: any) => e.kind === "ci_log");
  expect(ci.log_origin.binding).toMatchObject({
    repo: repository.toLowerCase(),
    run_id: runId,
    run_attempt: attempt,
    job_id: expectedJob,
    head_sha: expectedSha,
  });
  const selected = task.candidates.find((c: any) => c.selected);
  const independent = task.validations.find(
    (v: any) => v.kind === "independent",
  );
  expect(independent.candidate_id).toBe(selected.id);
  expect(independent.status).toBe("passed");
  expect(independent.target.patch_hash).toBe(selected.patch_hash);
  expect(independent.regression.patch_hash).toBe(selected.patch_hash);
  expect(independent.regression.counts.collected).toBe(
    task.baseline[1].counts.collected,
  );
  expect(
    task.executions.every(
      (e: any) =>
        e.isolation === "docker" &&
        e.head_sha === expectedSha &&
        e.termination.cleanup_confirmed,
    ),
  ).toBe(true);
  expect(
    task.executions.filter((e: any) => e.id === independent.target.id),
  ).toHaveLength(1);

  await page.reload();
  await expect(
    page.getByRole("heading", { name: "本地验证通过", exact: true }),
  ).toBeVisible();
  const reportUrl = await page
    .getByRole("link", { name: "下载报告" })
    .getAttribute("href");
  expect(reportUrl).toBeTruthy();
  const reportResponse = await page.request.get(reportUrl!);
  expect(reportResponse.status()).toBe(200);
  expect(reportResponse.headers()["content-disposition"]).toContain(
    "attachment",
  );
  const report = await reportResponse.json();
  expect(report.outcome).toBe("repaired");
  expect(report.head_sha).toBe(expectedSha);
  const patchUrl = `/api/v1/tasks/${id}/artifacts/${selected.patch_artifact_id}?download=true`;
  const patchResponse = await page.request.get(patchUrl);
  expect(patchResponse.status()).toBe(200);
  expect(
    createHash("sha256")
      .update(await patchResponse.body())
      .digest("hex"),
  ).toBe(selected.patch_hash);
  await page.getByRole("tab", { name: "策略记录" }).click();
  await expect(
    page.getByRole("heading", { name: "Harness² 策略优化" }),
  ).toBeVisible();
  await page.screenshot({
    path: resolve(root, "github-live-strategy.png"),
    fullPage: true,
    animations: "disabled",
  });
  const stranger = await context.browser()!.newContext();
  try {
    const other = await stranger.request.get(
      new URL(`/api/v1/tasks/${id}`, page.url()).toString(),
    );
    expect(other.status()).toBe(404);
    expect(
      (
        await stranger.request.get(new URL(reportUrl!, page.url()).toString())
      ).status(),
    ).toBe(404);
    expect(
      (
        await stranger.request.get(new URL(patchUrl, page.url()).toString())
      ).status(),
    ).toBe(404);
  } finally {
    await stranger.close();
  }
  const callsBefore = task.usage.calls;
  const tokensBefore = task.usage.tokens;
  let revalidation = task.validations.find(
    (v: any) => v.kind === "revalidation",
  );
  if (!revalidation) {
    await page.getByRole("tab", { name: /修复方案/ }).click();
    const validating = page.waitForResponse(
      (r) =>
        r.url().endsWith(`/api/v1/tasks/${id}/validations`) &&
        r.request().method() === "POST",
    );
    await page.getByRole("button", { name: "重新独立验证" }).click();
    const response = await validating;
    expect(response.status()).toBe(202);
    expect(response.request().postDataJSON().candidate_id).toBe(selected.id);
    revalidation = { id: (await response.json()).data.validation_id };
  }
  const validationId = revalidation.id;
  await expect
    .poll(
      async () => {
        task = (await (await page.request.get(`/api/v1/tasks/${id}`)).json())
          .data;
        return task.validations.find((v: any) => v.id === validationId)?.status;
      },
      { timeout: 60_000, intervals: [500, 1000, 2000] },
    )
    .toBe("passed");
  revalidation = task.validations.find((v: any) => v.id === validationId);
  expect(revalidation.target.id).not.toBe(independent.target.id);
  expect(revalidation.regression.id).not.toBe(independent.regression.id);
  expect(revalidation.target.patch_hash).toBe(selected.patch_hash);
  expect(revalidation.regression.patch_hash).toBe(selected.patch_hash);
  expect(task.usage.calls).toBe(callsBefore);
  expect(task.usage.tokens).toBe(tokensBefore);
  expect(task.outcome).toBe("repaired");
  await writeFile(taskFile, JSON.stringify(task, null, 2));
  await page.reload();
  await page.getByRole("tab", { name: "验证结果" }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("button", { name: "打开导航" })).toBeVisible();
  await expect(page.locator(".sidebar")).not.toBeInViewport();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
  ).toBe(false);
  await page.screenshot({
    path: resolve(root, "github-live-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
  expect(errors).toEqual([]);
  console.log(
    JSON.stringify({
      task_id: id,
      run_id: runId,
      job_id: expectedJob,
      head_sha: expectedSha,
      calls: task.usage.calls,
      tokens: task.usage.tokens,
      outcome: task.outcome,
    }),
  );
});
