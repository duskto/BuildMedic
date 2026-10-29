import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";

const plan = JSON.parse(
  readFileSync(
    new URL("../../../../evaluation/grouped-test.json", import.meta.url),
    "utf8",
  ),
);
const entry = {
  id: "a".repeat(32),
  source_case_id: "pagination",
  version: 1,
  cue: "synthetic development cue",
  lesson: "synthetic reusable guidance",
  model: "explicit-test-model",
};

for (const selectVersion of [true, false]) {
  test(`freeze explicit development guidance before registration (${selectVersion ? "selected" : "empty control"})`, async ({
    page,
  }) => {
    let manifest: Record<string, unknown> | null = null;
    const pack = {
      sha256: "f".repeat(64),
      version_ids: selectVersion ? [entry.id] : [],
      entries: selectVersion ? [entry] : [],
      training_usage: {
        tasks: selectVersion ? 1 : 0,
        calls: selectVersion ? 8 : 0,
        tokens: selectVersion ? 800 : 0,
        unknown_usage_tasks: 0,
        cost_usd: selectVersion ? null : 0,
      },
    };
    const report = () => ({
      id: "guidance-fixture",
      created_at: 1,
      status: "planned",
      manifest,
      manifest_sha256: "a".repeat(64),
      configuration_sha256: "b".repeat(64),
      report_sha256: "c".repeat(64),
      configuration: {
        provider: "fixture",
        model_id: "explicit-test-model",
        harness2_sha: "d".repeat(40),
      },
      scope: "public constructed frozen guidance fixture",
      environment_consistent: null,
      finished: 0,
      planned: 40,
      summary: [],
      rows: [],
      frozen_guidance: pack,
    });
    await page.route("**/api/v1/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      let data: unknown;
      if (path.endsWith("/health"))
        data = {
          model_ready: true,
          model_id: "explicit-test-model",
          adaptive_ready: true,
          adaptive_message: "fixture",
          runner: { ready: true, message: "fixture", backend: "docker" },
          scoring_ready: false,
        };
      else if (path.endsWith("/evaluation/cases")) data = [];
      else if (path.endsWith("/evaluation/grouped-plans"))
        data = { development: plan, test: plan };
      else if (path.endsWith("/evaluation/guidance-versions"))
        data = { items: [entry] };
      else if (
        path.endsWith("/experiments") &&
        route.request().method() === "POST"
      ) {
        manifest = route.request().postDataJSON();
        expect(manifest?.schema_version).toBe("4.0");
        expect(manifest?.guidance_version_ids).toEqual(
          selectVersion ? [entry.id] : [],
        );
        expect(manifest?.dataset_sha256).toBe(plan.dataset_sha256);
        expect(manifest?.cases).toEqual(plan.cases);
        expect(manifest?.experience).toBeUndefined();
        data = report();
      } else if (path.endsWith("/experiments"))
        data = { items: [], total: 0, page: 1, page_size: 10 };
      else if (path.endsWith("/experiments/guidance-fixture")) data = report();
      else if (path.endsWith("/analysis"))
        data = {
          source_report_sha256: "c".repeat(64),
          complete: false,
          environment_consistent: null,
          repeat_results: [],
          variation: [],
          pairs: [],
        };
      else if (path.endsWith("/score")) data = null;
      else
        throw new Error(
          `Unexpected request: ${route.request().method()} ${path}`,
        );
      await route.fulfill({
        json: { data, request_id: "explicit-guidance-fixture" },
      });
    });
    await page.goto("/experiments");
    await page
      .getByRole("combobox", { name: "实验组别", exact: true })
      .selectOption("4.0-test");
    const choice = page.getByRole("checkbox", { name: /pagination v1/ });
    await expect(choice).not.toBeChecked();
    if (selectVersion) await choice.check();
    else
      await expect(
        page.getByText("未选择开发指导，将冻结空指导。"),
      ).toBeVisible();
    await page
      .getByRole("button", { name: "登记对照清单", exact: true })
      .click();
    await expect(page).toHaveURL(/experiments\/guidance-fixture$/);
    const panel = page
      .locator("section")
      .filter({
        has: page.getByRole("heading", { name: "冻结开发指导", exact: true }),
      });
    await expect(panel).toContainText("三个修复组使用相同的只读指导");
    if (selectVersion) {
      await expect(panel.getByRole("link", { name: "v1" })).toHaveAttribute(
        "href",
        `/experience/${entry.id}`,
      );
      await expect(panel).toContainText("8 次模型调用");
      await expect(panel).toContainText("费用未知");
    } else
      await expect(panel).toContainText(
        "已冻结空指导，本批没有使用已发布经验。",
      );
    await page.setViewportSize({ width: 390, height: 844 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  });
}
