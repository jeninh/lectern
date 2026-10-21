import { defineConfig, devices } from "@playwright/test";

// End-to-end tests against a real server seeded by e2e/seed.py (see e2e/serve.sh). The suite
// shares one library and runs in order; specs that change it use their own books.
export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: "http://127.0.0.1:8090",
    viewport: { width: 1280, height: 800 },
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } } }],
  webServer: {
    command: "bash e2e/serve.sh",
    url: "http://127.0.0.1:8090/healthz",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
