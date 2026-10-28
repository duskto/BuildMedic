import { test, expect } from "@playwright/test";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

test.skip(
  process.env.BUILDMEDIC_LIVE !== "1",
  "Explicit opt-in: consumes real model usage.",
);

test("experience: real reflection publishes and a new task consumes its frozen Library", async ({
  page,
  context,
  browser,
}) => {
  test.setTimeout(480_000);
  const root = resolve(process.cwd(), "../../var/acceptance");
  await mkdir(root, { recursive: true });
  const snapshots: any[] = [];
  async function start() {
    await page.goto("/tasks/new?case=pagination");
    await page.getByRole("radio", { name: /自适应诊断/ }).check();
    await page
      .getByRole("checkbox", { name: /使用同域经验并整理任务反思/ })
      .check();
    const creating = page.waitForResponse(
      (r) =>
        r.url().endsWith("/api/v1/tasks") && r.request().method() === "POST",
    );
    await page.getByRole("button", { name: "开始诊断" }).click();
    const response = await creating;
    expect(response.status()).toBe(202);
    return (await response.json()).data.task_id as string;
  }
  async function finish(id: string, suffix: string, requireDraft = true) {
    let task: any;
    await expect
      .poll(
        async () => {
          task = (await (await page.request.get(`/api/v1/tasks/${id}`)).json())
            .data;
          return task.lifecycle;
        },
        { timeout: 215_000, intervals: [1000, 2000] },
      )
      .toMatch(/^(completed|failed|cancelled|interrupted)$/);
    await writeFile(
      resolve(root, `experience-${suffix}.json`),
      JSON.stringify(task, null, 2),
    );
    snapshots.push(task);
    expect(task.outcome, task.message).toBe("repaired");
    if (requireDraft) {
      expect(task.experience.reflection_status).toBe("available");
      expect(
        task.model_calls.filter(
          (c: any) =>
            c.role === "experience_reflection" && c.status === "completed",
        ),
      ).toHaveLength(1);
    } else {
      expect(["available", "no_entry", "rejected"]).toContain(
        task.experience.reflection_status,
      );
      if (task.experience.reflection_status !== "available")
        expect(task.experience.draft).toBeNull();
    }
    expect(task.rollouts).toHaveLength(4);
    expect(task.executions).toHaveLength(4 + 2 * task.candidates.length);
    const candidate = task.candidates.find((c: any) => c.selected);
    const independent = task.validations.find(
      (v: any) => v.kind === "independent",
    );
    expect(independent.status).toBe("passed");
    expect(independent.candidate_id).toBe(candidate.id);
    const candidateCheck = task.validations.find(
      (v: any) => v.kind === "candidate" && v.candidate_id === candidate.id,
    );
    expect(independent.target.id).not.toBe(candidateCheck.target.id);
    expect(independent.regression.id).not.toBe(candidateCheck.regression.id);
    expect(
      task.executions.every((e: any) => e.isolation === "docker"),
    ).toBeTruthy();
    return task;
  }
  const resumeId = process.env.BUILDMEDIC_EXPERIENCE_SOURCE_ID;
  if (resumeId) {
    const state = JSON.parse(
      await readFile(resolve(root, "experience-session.json"), "utf8"),
    );
    await context.addCookies(state.cookies);
    await page.goto(`/tasks/${resumeId}`);
  }
  const firstId = resumeId ?? (await start());
  await context.storageState({
    path: resolve(root, "experience-session.json"),
  });
  const first = await finish(firstId, "source");
  expect(first.experience.entries).toHaveLength(0);
  await page.getByRole("tab", { name: "策略记录" }).click();
  await expect(
    page.getByRole("heading", { name: "草稿已通过独立验证" }),
  ).toBeVisible();
  let version: any;
  if (first.experience.published_version_id) {
    version = (
      await (
        await page.request.get(
          `/api/v1/experience/${first.experience.published_version_id}`,
        )
      ).json()
    ).data;
  } else {
    const publishing = page.waitForResponse((r) =>
      r.url().endsWith(`/tasks/${firstId}/experience`),
    );
    await page.getByRole("button", { name: "发布到我的经验库" }).click();
    const published = await publishing;
    expect(published.status()).toBe(200);
    version = (await published.json()).data;
  }
  await page.getByRole("link", { name: "查看已发布经验" }).click();
  await expect(page.getByRole("heading", { name: version.cue })).toBeVisible();
  await page.getByText("查看完整采纳策略").click();
  await page.screenshot({
    path: resolve(root, "experience-version.png"),
    fullPage: true,
    animations: "disabled",
  });
  const outsider = await browser.newContext();
  expect(
    (
      await outsider.request.get(
        `http://127.0.0.1:8000/api/v1/experience/${version.id}`,
      )
    ).status(),
  ).toBe(404);
  await outsider.close();
  const secondId =
    process.env.BUILDMEDIC_EXPERIENCE_REUSE_ID ?? (await start());
  if (process.env.BUILDMEDIC_EXPERIENCE_REUSE_ID)
    await page.goto(`/tasks/${secondId}`);
  const second = await finish(secondId, "reuse", false);
  expect(second.experience.entries.map((e: any) => e.id)).toEqual([version.id]);
  await page.getByRole("tab", { name: "策略记录" }).click();
  await expect(
    page.getByRole("link", { name: new RegExp(`已绑定 v${version.version}`) }),
  ).toBeVisible();
  await page.getByRole("button", { name: "查看输入快照" }).click();
  await expect(page.locator(".experience-section pre")).toContainText(
    version.id,
  );
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: resolve(root, "experience-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.goto(`/experience/${version.id}`);
  await page
    .getByRole("button", { name: "撤销此版本，仅影响后续快照" })
    .click();
  await expect(page.getByText(/已撤销/)).toBeVisible();
  const frozen = (
    await (
      await page.request.get(
        `/api/v1/experience/snapshots/${second.experience.snapshot_id}`,
      )
    ).json()
  ).data;
  expect(frozen.content.entries[0].id).toBe(version.id);
  const after = await page.request.post("/api/v1/tasks", {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: {
      source: { type: "demo", case_id: "pagination" },
      mode: "adaptive",
      experience: true,
      limits: {
        max_model_calls: 12,
        max_tokens: 100000,
        max_wall_seconds: 600,
      },
    },
  });
  expect(after.status()).toBe(202);
  const thirdId = (await after.json()).data.task_id;
  await page.request.post(`/api/v1/tasks/${thirdId}/cancel`);
  const third = (
    await (await page.request.get(`/api/v1/tasks/${thirdId}`)).json()
  ).data;
  expect(third.experience.entries).toHaveLength(0);
  await writeFile(
    resolve(root, "experience-publication.json"),
    JSON.stringify(
      {
        version,
        frozen_hash: frozen.content_hash,
        after_revoke_task_id: thirdId,
      },
      null,
      2,
    ),
  );
  console.log(
    JSON.stringify({
      version_id: version.id,
      tasks: snapshots.map((t) => ({
        id: t.id,
        outcome: t.outcome,
        calls: t.usage.calls,
        tokens: t.usage.tokens,
        bound_entries: t.experience.entries.length,
        reflection_status: t.experience.reflection_status,
      })),
    }),
  );
});
