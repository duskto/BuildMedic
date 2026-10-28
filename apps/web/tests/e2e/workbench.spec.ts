import { test, expect } from "@playwright/test";

// Stable UI tests use explicit API fixtures. Live model integration is separate.
test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown;
    if (path.endsWith("/demo-cases"))
      data = [
        {
          id: "pagination",
          title: "分页边界错误",
          description: "分页结果缺少最后一项，检查切片边界。",
          category: "业务逻辑",
          repo: "local/buildmedic-demos",
          head_sha: null,
          snapshot_sha256: "a".repeat(64),
          profile_version: "python-pytest-demo-v1",
          execution_label: "本地真实案例",
        },
      ];
    else if (path.endsWith("/health"))
      data = {
        model_ready: false,
        model_id: null,
        runner: {
          ready: false,
          message: "测试环境未启动 Runner",
          backend: null,
        },
        adaptive_ready: false,
        adaptive_message: "待验收",
      };
    else if (path.endsWith("/stats"))
      data = { total: 0, active: 0, repaired: 0, blocked: 0 };
    else if (path.endsWith("/tasks") && route.request().method() === "GET")
      data = { items: [], total: 0, page: 1, page_size: 5 };
    else {
      await route.fulfill({
        status: 404,
        json: {
          error: { code: "fixture_missing", message: "No fixture" },
          request_id: "test",
        },
      });
      return;
    }
    await route.fulfill({ json: { data, request_id: "test-fixture" } });
  });
});

test("empty workbench displays actual empty statistics and configuration state", async ({
  page,
}) => {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "让失败的构建，有据可解。" }),
  ).toBeVisible();
  await expect(page.getByText("还没有诊断任务")).toBeVisible();
  await expect(page.getByText("运行环境需要配置")).toBeVisible();
  await page.getByRole("link", { name: "新建诊断" }).click();
  await expect(
    page.getByRole("heading", { name: "创建一次诊断" }),
  ).toBeVisible();
  await expect(page.getByText("a".repeat(64))).toBeVisible();
  await expect(page.getByText("自适应诊断", { exact: true })).toBeVisible();
});

test("task creation sends fixed case, server limits and idempotency key", async ({
  page,
}) => {
  let captured: Record<string, unknown> | undefined;
  await page.route("**/api/v1/tasks", async (route) => {
    if (route.request().method() !== "POST") {
      await route.fallback();
      return;
    }
    captured = route.request().postDataJSON();
    expect(route.request().headers()["idempotency-key"]).toBeTruthy();
    await route.fulfill({
      status: 409,
      json: {
        error: { code: "conflict", message: "测试用冲突，保留表单" },
        request_id: "test",
      },
    });
  });
  await page.goto("/tasks/new?case=pagination");
  await page.getByRole("button", { name: "开始诊断" }).click();
  await expect(page.getByRole("alert")).toContainText("测试用冲突");
  expect(captured?.source).toEqual({ type: "demo", case_id: "pagination" });
  expect(captured?.mode).toBe("standard");
  await expect(page.getByRole("radio", { name: /分页边界错误/ })).toBeChecked();
});

test("GitHub rate limits show a retry time and preserve the Run URL", async ({
  page,
}) => {
  let requests = 0;
  await page.route("**/api/v1/ci/preview", (route) => {
    requests++;
    return route.fulfill({
      status: 503,
      json: {
        error:
          requests === 1
            ? {
                code: "github_rate_limited",
                message: "GitHub 已限流，请在允许重试时间之后重试",
                retryable: true,
                details: { retry_at: 2000000000 },
              }
            : {
                code: "github_unavailable",
                message: "运行记录不可用",
                retryable: false,
                details: {},
              },
        request_id: "rate-limit-fixture",
      },
    });
  });
  await page.goto("/tasks/new");
  await page
    .getByRole("button", { name: "GitHub Actions", exact: true })
    .click();
  const url = "https://github.com/owner/repo/actions/runs/42?attempt=2";
  await page.getByLabel("Actions Run URL").fill(url);
  await page.getByRole("button", { name: "读取运行记录" }).click();
  await expect(page.getByRole("alert")).toContainText("可重试时间：");
  await expect(page.getByLabel("Actions Run URL")).toHaveValue(url);
  expect(requests).toBe(1);
  await page.getByRole("button", { name: "读取运行记录" }).click();
  await expect(page.getByRole("alert")).toContainText("运行记录不可用");
  await expect(page.getByText("可重试时间：")).toHaveCount(0);
  expect(requests).toBe(2);
});

test("mobile navigation opens and can reach task history", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByRole("button", { name: "打开导航" }).click();
  await page.getByRole("link", { name: "诊断任务", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "诊断任务", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "打开导航" })).toBeVisible();
});

test("model connection check stays on the server and displays an actionable failure", async ({
  page,
}) => {
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({
      json: {
        data: {
          model_ready: true,
          model_id: "deepseek-flash",
          runner: {
            ready: false,
            message: "Runner 尚未启动",
            backend: "docker",
          },
          adaptive_ready: false,
          adaptive_message: "待验收",
        },
        request_id: "probe-fixture",
      },
    }),
  );
  let checks = 0;
  await page.route("**/api/v1/model/check", async (route) => {
    checks += 1;
    expect(route.request().method()).toBe("POST");
    expect(route.request().headers()["authorization"]).toBeUndefined();
    expect(route.request().postData()).toBeNull();
    await route.fulfill({
      status: 502,
      json: {
        error: {
          code: "model_authentication",
          message: "模型密钥无效或已失效，请更新服务端密钥",
        },
        request_id: "probe-fixture",
      },
    });
  });
  await page.goto("/tasks/new");
  await expect(page.getByText("deepseek-flash", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "检查模型连接" }).click();
  await expect(page.getByRole("alert")).toContainText("模型密钥无效");
  expect(checks).toBe(1);
});

test("adaptive mode uses its shared budget and is disabled for remote logs", async ({
  page,
}) => {
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({
      json: {
        data: {
          model_ready: true,
          model_id: "test-model",
          runner: { ready: true, message: "fixture", backend: "docker" },
          adaptive_ready: true,
          adaptive_message: "fixture capability",
        },
        request_id: "fixture",
      },
    }),
  );
  let captured: any;
  await page.route("**/api/v1/tasks", async (route) => {
    if (route.request().method() !== "POST") {
      await route.fallback();
      return;
    }
    captured = route.request().postDataJSON();
    await route.fulfill({
      status: 409,
      json: {
        error: { code: "fixture", message: "测试提交已记录" },
        request_id: "fixture",
      },
    });
  });
  await page.goto("/tasks/new");
  const mode = page.getByRole("radio", { name: /自适应诊断/ });
  await expect(mode).toBeEnabled();
  await mode.check();
  const experience = page.getByRole("checkbox", {
    name: /使用同域经验并整理任务反思/,
  });
  await expect(experience).not.toBeChecked();
  await experience.check();
  await page.getByRole("button", { name: "开始诊断" }).click();
  await expect(page.getByRole("alert")).toContainText("测试提交已记录");
  expect(captured.mode).toBe("adaptive");
  expect(captured.experience).toBe(true);
  expect(captured.limits).toMatchObject({
    max_model_calls: 12,
    max_tokens: 150000,
  });
  await page.getByRole("radio", { name: /标准诊断/ }).check();
  await expect(experience).toHaveCount(0);
  await page.getByRole("button", { name: "开始诊断" }).click();
  await expect(page.getByRole("alert")).toContainText("测试提交已记录");
  expect(captured.experience).toBe(false);
  expect(captured.limits.max_tokens).toBe(100000);
  await mode.check();
  await expect(experience).not.toBeChecked();
  await page
    .getByRole("button", { name: "GitHub Actions", exact: true })
    .click();
  await expect(mode).toBeDisabled();
  await expect(experience).toHaveCount(0);
  await expect(page.getByRole("radio", { name: /标准诊断/ })).toBeChecked();
});

test("only a registered remote Job enables adaptive repair and pins its attempt", async ({
  page,
}) => {
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({
      json: {
        data: {
          model_ready: true,
          model_id: "fixture",
          runner: { ready: true, message: "fixture", backend: "docker" },
          adaptive_ready: true,
          adaptive_message: "fixture",
        },
        request_id: "fixture",
      },
    }),
  );
  await page.route("**/api/v1/ci/preview", (route) =>
    route.fulfill({
      json: {
        data: {
          repo: "owner/pytest-demo",
          run_id: 42,
          run_attempt: 2,
          head_sha: "d".repeat(40),
          execution_supported: true,
          message: "按登记 Job 进行独立复现，本地通过不代表远端 CI 成功",
          jobs: [
            {
              id: 17,
              name: "pytest",
              conclusion: "failure",
              execution_supported: true,
              profile_version: "registered-pytest-v1",
              failed_steps: [],
            },
            {
              id: 18,
              name: "lint",
              conclusion: "failure",
              execution_supported: false,
              profile_version: null,
              failed_steps: [],
            },
          ],
        },
        request_id: "fixture",
      },
    }),
  );
  let captured: any;
  await page.route("**/api/v1/tasks", async (route) => {
    captured = route.request().postDataJSON();
    await route.fulfill({
      status: 409,
      json: {
        error: { code: "fixture", message: "已记录远程任务输入" },
        request_id: "fixture",
      },
    });
  });
  await page.goto("/tasks/new");
  await page
    .getByRole("button", { name: "GitHub Actions", exact: true })
    .click();
  await page
    .getByLabel("Actions Run URL")
    .fill("https://github.com/owner/pytest-demo/actions/runs/42?attempt=2");
  await page.getByRole("button", { name: "读取运行记录" }).click();
  const mode = page.getByRole("radio", { name: /自适应诊断/ });
  await expect(mode).toBeDisabled();
  await page.getByRole("radio", { name: /pytest.*可独立复现/ }).check();
  await expect(mode).toBeEnabled();
  await mode.check();
  await expect(
    page.getByRole("checkbox", { name: /使用同域经验/ }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "开始诊断" }).click();
  await expect(page.getByRole("alert")).toContainText("已记录远程任务输入");
  expect(captured.source).toEqual({
    type: "github_actions",
    run_url: "https://github.com/owner/pytest-demo/actions/runs/42?attempt=2",
    job_id: 17,
    run_attempt: 2,
  });
  expect(captured.mode).toBe("adaptive");
  expect(captured.experience).toBe(false);
  await page.getByRole("radio", { name: /lint.*仅日志诊断/ }).check();
  await expect(mode).toBeDisabled();
  await expect(page.getByRole("radio", { name: /标准诊断/ })).toBeChecked();
});

test("agent tools are explicit and budgets reset when the mode changes", async ({
  page,
}) => {
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({
      json: {
        data: {
          model_ready: true,
          model_id: "fixture",
          runner: { ready: true, message: "fixture", backend: "docker" },
          adaptive_ready: true,
          adaptive_message: "fixture",
          agent_tools_ready: true,
          max_model_calls: 32,
        },
        request_id: "fixture",
      },
    }),
  );
  let captured: any;
  await page.route("**/api/v1/tasks", async (route) => {
    captured = route.request().postDataJSON();
    await route.fulfill({
      status: 409,
      json: {
        error: { code: "fixture", message: "已记录工具任务" },
        request_id: "fixture",
      },
    });
  });
  await page.goto("/tasks/new");
  const tools = page.getByRole("checkbox", { name: /启用 Agent 工具诊断/ });
  await expect(tools).not.toBeChecked();
  await tools.check();
  await expect(page.getByLabel("最多模型调用")).toHaveValue("16");
  await expect(page.getByLabel("Token 上限")).toHaveValue("150000");
  await page.getByRole("button", { name: "开始诊断" }).click();
  await expect(page.getByRole("alert")).toContainText("已记录工具任务");
  expect(captured.agent_tools).toBe(true);
  expect(captured.limits.max_model_calls).toBe(16);
  await page.getByRole("radio", { name: /自适应诊断/ }).check();
  await expect(page.getByLabel("最多模型调用")).toHaveValue("24");
  await expect(page.getByLabel("Token 上限")).toHaveValue("250000");
  await page.getByRole("checkbox", { name: /使用同域经验/ }).check();
  await expect(page.getByLabel("Token 上限")).toHaveValue("250000");
  await tools.uncheck();
  await expect(page.getByLabel("最多模型调用")).toHaveValue("12");
  await expect(page.getByLabel("Token 上限")).toHaveValue("150000");
  await page.getByRole("radio", { name: /标准诊断/ }).check();
  await expect(page.getByLabel("Token 上限")).toHaveValue("100000");
});
