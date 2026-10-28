import { test, expect } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

test.skip(
  process.env.BUILDMEDIC_LIVE !== "1",
  "Explicit opt-in: real provider usage.",
);

test("agent-tools: real DeepSeek native tools deliver a patch with independent Docker verification", async ({
  page,
  context,
}) => {
  test.setTimeout(240_000);
  const root = resolve(process.cwd(), "../../var/acceptance");
  await mkdir(root, { recursive: true });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/tasks/new?case=pagination");
  await page.getByRole("checkbox", { name: /启用 Agent 工具诊断/ }).check();
  await expect(page.getByLabel("最多模型调用")).toHaveValue("16");
  const creating = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/tasks") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "开始诊断" }).click();
  const created = await creating;
  expect(created.status()).toBe(202);
  expect(created.request().postDataJSON().agent_tools).toBe(true);
  const id = (await created.json()).data.task_id;
  await context.storageState({
    path: resolve(root, "agent-tools-session.json"),
  });
  let task: any;
  await expect
    .poll(
      async () => {
        task = (await (await page.request.get(`/api/v1/tasks/${id}`)).json())
          .data;
        return task.lifecycle;
      },
      { timeout: 210_000, intervals: [1000, 2000] },
    )
    .toMatch(/^(completed|failed|interrupted|cancelled)$/);
  await writeFile(
    resolve(root, "agent-tools.json"),
    JSON.stringify(task, null, 2),
  );
  expect(task.outcome, task.message).toBe("repaired");
  expect(task.usage.calls).toBeGreaterThan(1);
  expect(
    task.agent_actions.some(
      (a: any) => a.name === "read_repo_file" && a.status === "completed",
    ),
  ).toBeTruthy();
  expect(
    task.agent_actions.some(
      (a: any) => a.name === "submit_diagnosis" && a.status === "completed",
    ),
  ).toBeTruthy();
  const chosen = task.candidates.find((c: any) => c.selected);
  expect(chosen.delivery_format).toBe("agent_tools");
  const independent = task.validations.find(
    (v: any) => v.kind === "independent",
  );
  const candidate = task.validations.find(
    (v: any) => v.kind === "candidate" && v.candidate_id === chosen.id,
  );
  expect(independent.status).toBe("passed");
  expect(independent.candidate_id).toBe(chosen.id);
  expect(independent.target.id).not.toBe(candidate.target.id);
  expect(independent.regression.id).not.toBe(candidate.regression.id);
  expect(
    task.executions.every((e: any) => e.isolation === "docker"),
  ).toBeTruthy();
  await page.getByRole("tab", { name: "策略记录" }).click();
  await expect(
    page.getByRole("heading", { name: "Agent 工具调用" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "查看工具记录" }).first().click();
  await expect(page.locator(".agent-action pre").first()).toContainText(
    '"arguments"',
  );
  await page.screenshot({
    path: resolve(root, "agent-tools.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: resolve(root, "agent-tools-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
  expect(errors).toEqual([]);
  console.log(
    JSON.stringify({
      task_id: id,
      outcome: task.outcome,
      calls: task.usage.calls,
      tokens: task.usage.tokens,
      actions: task.agent_actions.map((a: any) => ({
        name: a.name,
        status: a.status,
      })),
      checks: task.executions.length,
    }),
  );
});
