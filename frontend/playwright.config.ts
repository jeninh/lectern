import { defineConfig, devices } from "@playwright/test";

// Two end-to-end suites, each against its own freshly seeded server serving the built app
// (`npm run build` first; `npm run e2e` runs both):
// - desktop: the Docs tab, the Library's folders, multi-select in the Library, Trash, and Review before filing (e2e/seed.py via
//   e2e/serve.sh, :8090; helpers in e2e/desktop-helpers.ts);
// - phones: the installed, offline app (docs/offline.md#tests), iPhone on WebKit and Android on
//   Chromium (e2e/serve.py, :8765; helpers in e2e/helpers.ts).
const PORT = Number(process.env.E2E_PORT ?? 8765);
const DESKTOP = ["docs", "folders", "library", "platform", "review", "trash"].map((n) => `${n}.spec.ts`);
const PHONES = ["app", "listening", "offline", "pdfreadalong", "sync"].map((n) => `${n}.spec.ts`);

export default defineConfig({
  testDir: "e2e",
  outputDir: "test-results",
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      testMatch: DESKTOP,
      use: { ...devices["Desktop Chrome"], baseURL: "http://127.0.0.1:8090", viewport: { width: 1280, height: 800 } },
    },
    {
      name: "iphone",
      testMatch: PHONES,
      use: { ...devices["iPhone 15"], browserName: "webkit", baseURL: `http://localhost:${PORT}`, serviceWorkers: "allow" },
      metadata: { user: "iphone" },
    },
    {
      name: "android",
      testMatch: PHONES,
      use: { ...devices["Pixel 7"], browserName: "chromium", baseURL: `http://localhost:${PORT}`, serviceWorkers: "allow" },
      metadata: { user: "android" },
    },
  ],
  webServer: [
    {
      command: "bash e2e/serve.sh",
      url: "http://127.0.0.1:8090/healthz",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: `uv run python e2e/serve.py ${PORT}`,
      url: `http://localhost:${PORT}/healthz`,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
});
