import { test, expect } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

test.skip(
  process.env.BUILDMEDIC_LIVE !== "1",
  "Explicit opt-in: this test incurs real model usage.",
);

test("adaptive: real Harness² proposes, runs and composes bounded strategies", async ({
  page,
  context,
}) => {
  test.setTimeout(240_000);
  const root = resolve(process.cwd(), "../../var/acceptance");
  await mkdir(root, { recursive: true });
  await page.goto("/tasks/new?case=pagination");
  const mode = page.getByRole("radio", { name: /自适应诊断/ });
  await expect(mode).toBeEnabled();
  await mode.check();
  await expect(page.getByLabel("最多模型调用")).toHaveValue("12");
  await expect(page.getByLabel("Token 上限")).toHaveValue("100000");
  const creating = page.waitForResponse(
    (r) => r.url().endsWith("/api/v1/tasks") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "开始诊断" }).click();
  const created = await creating;
  expect(created.status()).toBe(202);
  expect(created.request().postDataJSON().mode).toBe("adaptive");
  const id = (await created.json()).data.task_id;
  await expect(page).toHaveURL(new RegExp(`/tasks/${id}$`));
  await context.storageState({ path: resolve(root, "harness-session.json") });
  let task: any;
  await expect
    .poll(
      async () => {
        task = (await (await page.request.get(`/api/v1/tasks/${id}`)).json())
          .data;
        return task.lifecycle;
      },
      { timeout: 200_000, intervals: [1000, 2000] },
    )
    .toMatch(/^(completed|failed|cancelled|interrupted)$/);
  await writeFile(
    resolve(root, "harness-adaptive.json"),
    JSON.stringify(task, null, 2),
  );
  expect(task.outcome, task.message).toBe("repaired");
  expect(task.harness2_sha).toBe("1cfe254e8d25b15f37048c3730134686abe60335");
  expect(task.harness.selected_label).toBe("c1");
  expect(task.harness.stages).toHaveLength(3);
  expect(task.rollouts.map((r: any) => r.label).sort()).toEqual([
    "base1",
    "c1",
    "dom1",
    "inv1",
  ]);
  expect(
    [
      ...new Set(
        task.model_calls
          .filter((r: any) => r.role === "harness_proposer")
          .map((r: any) => r.label.split(".")[0]),
      ),
    ].sort(),
  ).toEqual(["c1", "dom1", "inv1"]);
  expect(task.harness.proposals.some((r: any) => r.changed)).toBeTruthy();
  expect(task.executions).toHaveLength(12);
  expect(
    task.executions.every((r: any) => r.isolation === "docker"),
  ).toBeTruthy();
  const candidate = task.candidates.find((c: any) => c.selected);
  expect(candidate.rollout_id).toBe(task.rollouts[3].id);
  expect(
    task.validations.find((v: any) => v.kind === "independent").candidate_id,
  ).toBe(candidate.id);
  await expect(
    page.getByRole("heading", { name: "本地验证通过", exact: true }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "策略记录" }).click();
  await expect(
    page.getByRole("heading", { name: "Harness² 策略优化" }),
  ).toBeVisible();
  await expect(page.getByText(/采用策略 c1/)).toBeVisible();
  await page.getByRole("button", { name: "查看策略差异" }).first().click();
  await expect(page.locator(".harness-diff pre").first()).toContainText(
    "--- a/",
  );
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: resolve(root, "harness-strategy.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.reload();
  await page.getByRole("tab", { name: "策略记录" }).click();
  await expect(page.getByText(/采用策略 c1/)).toBeVisible();
  await context.storageState({ path: resolve(root, "harness-session.json") });
  console.log(
    JSON.stringify({
      task_id: id,
      outcome: task.outcome,
      model_calls: task.usage.calls,
      tokens: task.usage.tokens,
      stages: task.harness.stages.map((s: any) => ({
        label: s.label,
        kept_base: s.kept_base,
        fell_back: s.fell_back,
      })),
      image: task.executions[0].image_digest,
    }),
  );
});
