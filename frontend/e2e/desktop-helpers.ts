import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";

export const PASSWORD = "e2e correct horse battery"; // e2e/seed.py

/** The flat Everything view of the Library is where the multi-select suites run (the default
 * Folders view shows only books on no shelf); e2e/folders.spec.ts covers Folders. */
export async function signIn(page: Page, path = "/app/library?view=everything") {
  // The password form's POST, from the page's own cookie jar (the form itself sits behind
  // "Use password instead").
  const base = test.info().project.use.baseURL!;
  const r = await page.request.post("/login", { form: { username: "owner", password: PASSWORD, totp: "", next: "/app" }, headers: { origin: base }, maxRedirects: 0 });
  expect(r.status(), await r.text()).toBe(303);
  await page.goto(path);
}

export const grid = (page: Page) => page.getByRole("grid").first();
/** A Library cell by its book title. */
export const book = (page: Page, title: string) => page.getByRole("gridcell", { name: new RegExp(`^${title} by `) });
/** Clicks a cell's selection circle (shown on hover): selects it, or toggles it, without opening. */
export async function circle(cell: Locator) {
  await cell.hover();
  await cell.locator("[data-sel-toggle]").click();
}
export const selectedTitles = async (page: Page) =>
  (await page.locator('[role="gridcell"][aria-selected="true"]').evaluateAll((els) => els.map((e) => e.getAttribute("aria-label")?.split(" by ")[0] ?? "")))
    .sort();
export const countText = (page: Page) => page.getByTestId("selection-count");
export const focusedLabel = (page: Page) => page.evaluate(() => document.activeElement?.getAttribute("aria-label")?.split(" by ")[0] ?? document.activeElement?.tagName);

export const titles = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => `Book ${String(from + i).padStart(3, "0")}`);

/** The platform's command modifier for this page ("Meta" when the page thinks it is a Mac). */
export const MOD = "Control";

/** Writes from the test: same-origin + the session's CSRF token. */
export async function api(page: Page) {
  const me = await (await page.request.get("/api/me")).json();
  const headers = { "x-csrf-token": me.csrf_token, origin: new URL(page.url()).origin };
  const r: APIRequestContext = page.request;
  return {
    get: async (url: string) => (await r.get(url)).json(),
    post: async (url: string, data: unknown) => {
      const res = await r.post(url, { data, headers });
      expect(res.ok(), await res.text()).toBeTruthy();
      return res.json();
    },
  };
}

export async function library(page: Page): Promise<{ id: number; title: string; tags: string[] }[]> {
  return (await (await page.request.get("/api/library")).json()).works;
}

export async function idOf(page: Page, title: string): Promise<number> {
  const w = (await library(page)).find((x) => x.title === title);
  if (!w) throw new Error(`no ${title}`);
  return w.id;
}

export async function isRendered(loc: Locator) {
  return (await loc.count()) > 0;
}
