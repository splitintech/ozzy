import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/browser",
  fullyParallel: false,
  workers: 1,
  timeout: 180_000,
  expect: { timeout: 45_000 },
  reporter: [["list"], ["html", { outputFolder: "artifacts/browser/report", open: "never" }]],
  outputDir: "artifacts/browser/results",
  use: {
    baseURL: "http://127.0.0.1:18765",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "chromium-desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 1080 } } },
    { name: "chromium-mobile", use: { ...devices["Pixel 7"] } },
  ],
  webServer: {
    command: "npm run build && .venv/bin/python scripts/browser_test_server.py",
    url: "http://127.0.0.1:18765/health",
    reuseExistingServer: false,
    timeout: 90_000,
  },
});
