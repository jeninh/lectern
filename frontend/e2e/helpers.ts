import { expect, type APIRequestContext, type Page, type Route, type TestInfo, request as pwRequest } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

export const PASSWORD = "correct horse battery staple e2e";
export const MAGI = "The Gift of the Magi";

/** The seeded reader for this project (playwright.config.ts metadata). */
export const userOf = (info: TestInfo) => info.project.metadata.user as string;

export async function signIn(page: Page, username: string) {
  // The login page prefetches a passkey challenge, which counts against the per-IP passkey rate
  // limit; password sign-ins don't need one, so many of them in a row must not use it up.
  const skip = (r: Route) => r.abort();
  await page.route("**/login/passkey/options", skip);
  await page.goto("/login");
  await page.getByText("Use password instead").click();
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.waitForURL(/\/app/);
  await page.unroute("**/login/passkey/options", skip);
  await expect(page.locator(".user-chip:visible, .bottom-nav:visible").first()).toBeVisible();
}

/** Wait until the service worker controls the page (first visit: after it activates). */
export async function controlled(page: Page) {
  await page.waitForFunction(() => navigator.serviceWorker.ready.then(() => true));
  if (!(await page.evaluate(() => !!navigator.serviceWorker.controller))) {
    await page.reload();
    await page.waitForFunction(() => !!navigator.serviceWorker.controller);
  }
}

/** A signed-in API client for the same person on "another device", or the owner. */
export async function apiAs(baseURL: string, username: string, device = "Kobo"): Promise<{ api: APIRequestContext; csrf: string; put: (url: string, body: unknown) => Promise<unknown>; del: (url: string) => Promise<number> }> {
  const api = await pwRequest.newContext({ baseURL, extraHTTPHeaders: { origin: baseURL, "user-agent": device } });
  const r = await api.post("/login", { form: { username, password: PASSWORD, totp: "" }, maxRedirects: 0 });
  expect(r.status()).toBe(303);
  const csrf = (await (await api.get("/api/me")).json()).csrf_token as string;
  return {
    api,
    csrf,
    put: async (url, body) => (await api.put(url, { data: body, headers: { "x-csrf-token": csrf } })).json(),
    del: async (url) => (await api.delete(url, { headers: { "x-csrf-token": csrf } })).status(),
  };
}

export async function workId(page: Page, title: string): Promise<number> {
  const works = await page.evaluate(async () => (await (await fetch("/api/library")).json()).works as { id: number; title: string }[]);
  return works.find((w) => w.title === title)!.id;
}

/** Download a book from its details page with the given choices left as defaults. */
export async function downloadBook(page: Page, id: number, opts: { fullQuality?: boolean } = {}) {
  await page.goto(`/app/library/${id}`);
  await page.getByTestId("download-offline").click();
  const start = page.getByTestId("download-start");
  await expect(start).toBeEnabled();
  if (opts.fullQuality) await page.getByRole("switch", { name: /Full quality/ }).click();
  await start.click();
  await expect(page.locator(".toast").filter({ hasText: "is on this device" })).toBeVisible({ timeout: 30_000 });
}

export async function cacheKeys(page: Page): Promise<Record<string, string[]>> {
  return page.evaluate(async () => {
    const out: Record<string, string[]> = {};
    for (const name of await caches.keys()) out[name] = (await (await caches.open(name)).keys()).map((r) => new URL(r.url).pathname + new URL(r.url).search);
    return out;
  });
}

/** Screenshots for docs/screenshots (copied there by hand; see docs/offline.md#tests). */
export async function shot(page: Page, info: TestInfo, name: string) {
  const dir = resolve(info.project.testDir, "..", "e2e-screenshots");
  mkdirSync(dir, { recursive: true });
  await page.screenshot({ path: resolve(dir, `${info.project.name}-${name}.png`) });
}
