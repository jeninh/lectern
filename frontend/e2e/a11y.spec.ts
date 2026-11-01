// Accessibility (#141): axe over the main pages in both app themes and over the reader in every
// reader theme (#79), plus keyboard checks for the reader's panel and the shared dialog pattern.
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { signIn } from "./desktop-helpers";

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

/** Violations on the page as it is now, each with the first few offending elements. */
async function violations(page: Page, what: string) {
  const { violations } = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  return violations.map((v) => `${what}: ${v.id} (${v.impact}): ${v.help}\n${v.nodes.slice(0, 4).map((n) => `    ${n.target.join(" ")} :: ${(n.any[0] ?? n.all[0] ?? n.none[0])?.message}`).join("\n")}`);
}
async function scan(page: Page, what: string) {
  expect(await violations(page, what)).toEqual([]);
}

// Themes cross-fade over 320ms; axe must read the colours the page settles on, not the middle.
test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
});

const PAGES = ["/app", "/app/library", "/app/library?view=everything", "/app/jobs", "/app/devices", "/app/listening", "/app/downloads", "/app/highlights", "/app/vocabulary", "/app/security", "/app/activity", "/app/stats", "/app/docs", "/app/admin", "/app/health", "/app/trash"];

for (const theme of ["light", "dark"]) {
  test(`axe: main pages, ${theme} app theme`, async ({ page }) => {
    await page.addInitScript((t) => localStorage.setItem("lectern.theme", JSON.stringify(t)), theme);
    await signIn(page, "/app");
    const found: string[] = [];
    for (const path of PAGES) {
      await page.goto(path);
      await expect(page.locator("main h1").first()).toBeVisible();
      found.push(...(await violations(page, `${path} (${theme})`)));
    }
    // A book's details page.
    await page.goto("/app/library?view=everything");
    await page.getByRole("gridcell").first().click();
    await expect(page).toHaveURL(/\/app\/library\/\d+/);
    found.push(...(await violations(page, `book details (${theme})`)));
    expect(found).toEqual([]);
  });
}

test("axe: the login page", async ({ page }) => {
  await page.goto("/login");
  await scan(page, "login");
  await page.getByText("Use password instead").click();
  await scan(page, "login, password form");
});

async function openBook(page: Page) {
  await signIn(page, "/app");
  const works = await page.evaluate(async () => (await (await fetch("/api/library")).json()).works as { id: number; title: string }[]);
  await page.goto(`/app/reader/${works.find((w) => w.title === "Book 001")!.id}`);
  await expect(page.getByRole("button", { name: "Text and layout" })).toBeVisible();
}

for (const [theme, label, app] of [["auto", "Match app", "light"], ["auto", "Match app", "dark"], ["light", "Light", "light"], ["sepia", "Warm", "light"], ["dark", "Warm dark", "light"], ["contrast", "High contrast", "light"]]) {
  test(`axe: reader chrome and panel, ${theme} theme in the ${app} app`, async ({ page }) => {
    await page.addInitScript((t) => localStorage.setItem("lectern.theme", JSON.stringify(t)), app);
    await openBook(page);
    await page.getByRole("button", { name: "Text and layout" }).click();
    const panel = page.getByRole("dialog", { name: /Text and layout/ });
    await panel.getByRole("radio", { name: label, exact: true }).click();
    await expect(page.locator(".reader")).toHaveAttribute("data-reader-theme", theme === "auto" ? app : theme);
    await scan(page, `reader panel (${theme}, ${app} app)`);
    await page.keyboard.press("Escape");
    await expect(panel).toBeHidden();
    await scan(page, `reader chrome (${theme}, ${app} app)`);
  });
}

test("reader: panel traps focus, Escape closes it and returns focus", async ({ page }) => {
  await openBook(page);
  const opener = page.getByRole("button", { name: "Text and layout" });
  await opener.focus();
  await page.keyboard.press("Enter");
  const panel = page.getByRole("dialog", { name: /Text and layout/ });
  await expect(panel).toBeVisible();
  for (let i = 0; i < 40; i++) {
    await page.keyboard.press("Tab");
    if (i === 4) await page.screenshot({ path: "test-results/a11y-focus-reader-panel.png" });
    expect(await panel.evaluate((p) => p.contains(document.activeElement))).toBe(true);
  }
  for (let i = 0; i < 40; i++) {
    await page.keyboard.press("Shift+Tab");
    expect(await panel.evaluate((p) => p.contains(document.activeElement))).toBe(true);
  }
  await page.keyboard.press("Escape");
  await expect(panel).toBeHidden();
  await expect(opener).toBeFocused();
});

test("every control in the app shell has a name and a visible focus ring", async ({ page }) => {
  await signIn(page, "/app");
  const seen: string[] = [];
  const bad: string[] = [];
  for (let i = 0; i < 60; i++) {
    await page.keyboard.press("Tab");
    const info = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement;
      if (!el || el === document.body) return null;
      const cs = getComputedStyle(el);
      const name = el.getAttribute("aria-label") || el.getAttribute("aria-labelledby") || el.textContent?.trim() || el.getAttribute("title") || "";
      const ring = (cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) > 0) || cs.boxShadow !== "none";
      return { id: `${el.tagName.toLowerCase()}.${el.className}`, name, ring };
    });
    if (!info) continue;
    if (i === 3) await page.screenshot({ path: "test-results/a11y-focus-nav.png" });
    seen.push(info.id);
    if (!info.name) bad.push(`${info.id}: no accessible name`);
    if (!info.ring) bad.push(`${info.id}: no visible focus indicator`);
  }
  expect(seen.length).toBeGreaterThan(10);
  expect(bad).toEqual([]);
});

test("the command palette and the New menu pass axe, trap focus, and give it back on Escape", async ({ page }) => {
  await signIn(page, "/app/library");
  await page.getByRole("button", { name: /^Search/ }).first().focus();
  await page.keyboard.press("Enter");
  const palette = page.getByRole("dialog").first();
  await expect(palette).toBeVisible();
  await expect(page.getByRole("combobox").first()).toBeFocused();
  await scan(page, "command palette");
  await page.keyboard.press("Escape");
  await expect(palette).toBeHidden();
  await expect(page.getByRole("button", { name: /^Search/ }).first()).toBeFocused();

  const menuButton = page.getByRole("button", { name: "New: add a shelf or auto-shelves" });
  await menuButton.focus();
  await page.keyboard.press("ArrowDown");
  await expect(page.getByRole("menuitem").first()).toBeFocused();
  await scan(page, "New menu");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toBeHidden();
  await expect(menuButton).toBeFocused();
});
