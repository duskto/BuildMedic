import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/live",
  outputDir: "test-results/live",
  fullyParallel: false,
  workers: 1,
  timeout: 180_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: process.env.BUILDMEDIC_ACCEPTANCE_URL ?? "http://127.0.0.1:8000",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    viewport: { width: 1440, height: 1000 },
  },
  projects: [{ name: "live-chromium", use: { browserName: "chromium" } }],
});
