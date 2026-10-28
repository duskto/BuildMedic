import { test, expect } from "@playwright/test";

// These are explicit contract fixtures, separate from paid live-provider acceptance.
function snapshot(lifecycle = "completed") {
  return {
    id: "fixture-task",
    title: "详情交互测试",
    repo: "local/test-fixture",
    case_id: "pagination",
    source_type: "demo",
    head_sha: null,
    snapshot_sha256: "a".repeat(64),
    run_id: null,
    run_attempt: null,
    job_id: null,
    profile_version: "fixture-profile",
    execution_label: "浏览器测试数据",
    mode: "standard",
    lifecycle,
    phase: lifecycle === "completed" ? "report" : "reproduce",
    phase_history: ["ingest", "preflight", "reproduce"],
    outcome: null,
    message: "可恢复的测试任务",
    state_version: 1,
    last_event_seq: 1,
    created_at: 1,
    parent_task_id: null,
    validation_status: "not_run",
    reproduction_status: "blocked",
    usage: { calls: 0, tokens: 0, cost_usd: null, usage_known: true },
    limits: { max_wall_seconds: 600, max_model_calls: 8, max_tokens: 48000 },
    evidence: [],
    candidates: [],
    validations: [],
    executions: [],
    rollouts: [],
    diagnosis: null,
    artifacts: [],
  };
}

test("temporary polling failure preserves the task and recovers from the authoritative snapshot", async ({
  page,
}) => {
  let failing = false;
  let failures = 0;
  let task = snapshot("running");
  await page.route("**/api/v1/tasks/fixture-task/events?*", (route) =>
    route.abort(),
  );
  await page.route("**/api/v1/tasks/fixture-task", async (route) => {
    if (failing) {
      failures++;
      await route.abort("internetdisconnected");
      return;
    }
    await route.fulfill({ json: { data: task, request_id: "fixture" } });
  });
  await page.goto("/tasks/fixture-task");
  await expect(page.getByRole("heading", { name: task.title })).toBeVisible();
  failing = true;
  await expect.poll(() => failures).toBeGreaterThanOrEqual(2);
  await expect(page.getByRole("heading", { name: task.title })).toBeVisible();
  task = { ...snapshot("completed"), message: "已恢复并读取最新快照" };
  failing = false;
  await expect(page.getByText(task.message)).toBeVisible();
  await page.reload();
  await expect(page.getByText(task.message)).toBeVisible();
});

test("retry retains its idempotency key after a lost response and resets detail tabs for the new task", async ({
  page,
}) => {
  const task = snapshot();
  const keys: string[] = [];
  await page.route("**/api/v1/tasks/*", (route) =>
    route.fulfill({
      json: {
        data: {
          ...task,
          id: new URL(route.request().url()).pathname.split("/").pop(),
        },
        request_id: "fixture",
      },
    }),
  );
  await page.route("**/api/v1/tasks/fixture-task/retry", async (route) => {
    keys.push(route.request().headers()["idempotency-key"]);
    if (keys.length === 1)
      await route.fulfill({
        status: 503,
        json: {
          error: { code: "network_fixture", message: "暂时不可用，请重试" },
          request_id: "fixture",
        },
      });
    else
      await route.fulfill({
        status: 202,
        json: { data: { task_id: "retried-task" }, request_id: "fixture" },
      });
  });
  await page.goto("/tasks/fixture-task");
  await page.getByRole("tab", { name: "策略记录" }).click();
  await page.getByRole("button", { name: "重新诊断", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("暂时不可用");
  await page.getByRole("button", { name: "重新诊断", exact: true }).click();
  await expect(page).toHaveURL(/\/tasks\/retried-task$/);
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBeTruthy();
  expect(keys[1]).toBe(keys[0]);
  await expect(page.getByRole("tab", { name: "概览" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
});

test("adaptive detail shows real stage records, fallback, adoption and strategy diff", async ({
  page,
}) => {
  const task = {
    ...snapshot(),
    mode: "adaptive",
    harness2_sha: "a".repeat(40),
    harness: {
      status: "completed",
      k: 1,
      mode: "seq",
      bridge: "buildmedic_http",
      base_hash: "a".repeat(64),
      selected_label: "c1",
      selection_reason: "唯一组合最终结果",
      proposals: [
        {
          label: "inv1.a1",
          before_hash: "a".repeat(64),
          after_hash: "b".repeat(64),
          changed: true,
          notes: "定位调用契约",
          diff_artifact_id: "strategy-diff",
          model_call_id: "call-1",
        },
      ],
      stages: [
        {
          label: "inv1",
          dim: "INV",
          fell_back: false,
          kept_base: false,
          attempts: 1,
          notes: "实际修改",
          error: "",
        },
        {
          label: "dom1",
          dim: "DOM",
          fell_back: true,
          kept_base: false,
          attempts: 1,
          notes: "",
          error: "提议格式错误，已保留基准",
        },
      ],
    },
  };
  await page.route("**/api/v1/tasks/fixture-task", (route) =>
    route.fulfill({ json: { data: task, request_id: "fixture" } }),
  );
  await page.route(
    "**/api/v1/tasks/fixture-task/artifacts/strategy-diff",
    (route) =>
      route.fulfill({
        body: "--- a/systemprompt.md\n+++ b/systemprompt.md\n-old guidance\n+new guidance\n",
        contentType: "text/plain",
      }),
  );
  await page.goto("/tasks/fixture-task");
  await page.getByRole("tab", { name: "策略记录" }).click();
  await expect(
    page.getByRole("heading", { name: "Harness² 策略优化" }),
  ).toBeVisible();
  await expect(page.getByText(/采用策略 c1/)).toBeVisible();
  await expect(
    page.getByText("提议失败，继承原策略", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "查看策略差异" }).click();
  await expect(page.locator(".harness-diff pre")).toContainText(
    "+new guidance",
  );
});

test("experience draft publishes with a stable key and retains frozen inputs after revocation", async ({
  page,
}) => {
  const version = {
    id: "experience-v1",
    domain: "业务逻辑",
    version: 1,
    source_task_id: "fixture-task",
    status: "active",
    created_at: 1,
    content_hash: "d".repeat(64),
    cue: "切片边界不一致",
    lesson: "交叉检查调用者的边界约定。",
    evidence_ids: ["ev-boundary"],
    scope: "development_only",
  };
  let task = {
    ...snapshot(),
    outcome: "repaired",
    mode: "adaptive",
    validation_status: "passed",
    experience: {
      enabled: true,
      snapshot_id: "input-v0",
      snapshot_hash: "e".repeat(64),
      domain: "业务逻辑",
      head_version: 0,
      entries: [],
      reflection_status: "available",
      published_version_id: null as string | null,
      draft: {
        artifact_id: "draft",
        cue: version.cue,
        lesson: version.lesson,
        evidence_ids: version.evidence_ids,
        model_call_id: "reflection-1",
      },
    },
  };
  const keys: string[] = [];
  await page.route("**/api/v1/tasks/fixture-task", (route) =>
    route.fulfill({ json: { data: task, request_id: "fixture" } }),
  );
  await page.route("**/api/v1/experience/snapshots/input-v0", (route) =>
    route.fulfill({
      json: {
        data: { content_hash: "e".repeat(64), content: { entries: [] } },
        request_id: "fixture",
      },
    }),
  );
  await page.route("**/api/v1/tasks/fixture-task/experience", async (route) => {
    keys.push(route.request().headers()["idempotency-key"]);
    if (keys.length === 1)
      await route.fulfill({
        status: 503,
        json: {
          error: { code: "fixture", message: "发布响应丢失，请重试" },
          request_id: "fixture",
        },
      });
    else {
      task = {
        ...task,
        experience: { ...task.experience, published_version_id: version.id },
      };
      await route.fulfill({ json: { data: version, request_id: "fixture" } });
    }
  });
  const content = {
    tree: { "systemprompt.md": "bound strategy" },
    edits: "交叉定位边界",
    harness_hash: "a".repeat(64),
    profile_hash: "a".repeat(64),
    harness2_sha: "a".repeat(40),
    patch_hash: "b".repeat(64),
    candidate_id: "patch-1",
    validation_id: "independent-1",
    model_call_id: "reflection-1",
    input_snapshot_id: "input-v0",
    input_snapshot_hash: "e".repeat(64),
    parent_version_ids: [],
    model: "fixture",
    response_model: null,
    case_id: "pagination",
  };
  let revoked = false;
  await page.route("**/api/v1/experience/experience-v1", (route) =>
    route.fulfill({
      json: {
        data: { ...version, status: revoked ? "revoked" : "active", content },
        request_id: "fixture",
      },
    }),
  );
  await page.route(
    "**/api/v1/experience/experience-v1/revoke",
    async (route) => {
      revoked = true;
      await route.fulfill({
        json: {
          data: { ...version, status: "revoked" },
          request_id: "fixture",
        },
      });
    },
  );
  await page.goto("/tasks/fixture-task?tab=strategy");
  await expect(
    page.getByText("空经验快照，本任务未读取历史经验。"),
  ).toBeVisible();
  await page.getByRole("button", { name: "查看输入快照" }).click();
  await expect(page.locator(".experience-section pre")).toContainText(
    '"entries": []',
  );
  await page.getByRole("button", { name: "发布到我的经验库" }).click();
  await expect(page.getByRole("alert")).toContainText("发布响应丢失");
  await page.getByRole("button", { name: "发布到我的经验库" }).click();
  await page.getByRole("link", { name: "查看已发布经验" }).click();
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBeTruthy();
  expect(keys[0]).toBe(keys[1]);
  await expect(page.getByRole("heading", { name: version.cue })).toBeVisible();
  await page.getByText("查看完整采纳策略").click();
  await expect(page.getByText("bound strategy", { exact: true })).toBeVisible();
  await page
    .getByRole("button", { name: "撤销此版本，仅影响后续快照" })
    .click();
  await expect(page.getByText(/业务逻辑 · v1 · 已撤销/)).toBeVisible();
  await page.reload();
  await expect(page.getByText(/业务逻辑 · v1 · 已撤销/)).toBeVisible();
  await page.getByRole("link", { name: "查看来源任务" }).click();
  await expect(page.getByRole("tab", { name: "策略记录" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(
    page.getByText("空经验快照，本任务未读取历史经验。"),
  ).toBeVisible();
});

test("empty private experience library is useful on a mobile screen", async ({
  page,
}) => {
  await page.route("**/api/v1/experience?*", (route) =>
    route.fulfill({
      json: {
        data: { items: [], total: 0, page: 1, page_size: 20 },
        request_id: "fixture",
      },
    }),
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/experience");
  await expect(
    page.getByRole("heading", { name: "还没有发布的经验" }),
  ).toBeVisible();
  await page.getByLabel("筛选领域").fill("业务逻辑");
  await expect(
    page.getByRole("heading", { name: "还没有发布的经验" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("agent tool records distinguish completed reads and rejected operations", async ({
  page,
}) => {
  const task = {
    ...snapshot(),
    agent_tools: true,
    agent_actions: [
      {
        id: "action-1",
        name: "read_repo_file",
        status: "completed",
        model_call_id: "model-1",
        rollout_id: "rollout-1",
        round: 1,
        started_at: 1,
        finished_at: 2,
        request_artifact_id: "tool-request",
        result_artifact_id: "tool-result",
      },
      {
        id: "action-2",
        name: "run_check",
        status: "rejected",
        model_call_id: "model-1",
        rollout_id: "rollout-1",
        round: 1,
        started_at: 1,
        finished_at: 2,
        request_artifact_id: "bad-request",
        result_artifact_id: "bad-result",
        error_code: "candidate_not_allowed",
      },
    ],
  };
  await page.route("**/api/v1/tasks/fixture-task", (route) =>
    route.fulfill({ json: { data: task, request_id: "fixture" } }),
  );
  await page.route(
    "**/api/v1/tasks/fixture-task/artifacts/tool-request",
    (route) =>
      route.fulfill({
        body: '{"name":"read_repo_file","arguments":{"path":"src/demo.py"}}',
        contentType: "application/json",
      }),
  );
  await page.route(
    "**/api/v1/tasks/fixture-task/artifacts/tool-result",
    (route) =>
      route.fulfill({
        body: '{"ok":true,"result":{"evidence_ids":["ev-code-1"]}}',
        contentType: "application/json",
      }),
  );
  await page.goto("/tasks/fixture-task?tab=strategy");
  await expect(
    page.getByRole("heading", { name: "Agent 工具调用" }),
  ).toBeVisible();
  await expect(page.locator(".agent-action").nth(1)).toContainText("已拒绝");
  await expect(page.locator(".agent-action").nth(1)).toContainText(
    "candidate_not_allowed",
  );
  await page.getByRole("button", { name: "查看工具记录" }).first().click();
  await expect(page.locator(".agent-action").first()).toContainText(
    "src/demo.py",
  );
  await expect(page.locator(".agent-action").first()).toContainText(
    "ev-code-1",
  );
});
