import { defineConfig, devices } from "@playwright/test";

// End-to-end tests run against a running stack:
//   PostgreSQL + backend (STUCK_AFTER_S=6, HANDOVER_WINDOW_S=3) + mock OPC UA server with
//   control API (python -m dmf_mock --mode idle --control-port 8765) + built frontend.
// See docs/06-testing.md ("End-to-end tests") for the exact commands; CI starts the same stack.
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1, // all tests share one (mock) plant
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:4173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    viewport: { width: 1280, height: 1000 },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 1000 } } }],
});
