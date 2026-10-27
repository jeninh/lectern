// Library filters and smart shelves (docs/filters.md), against e2e/seed.py: 360 books, of which
// only "Book 001" … "Book 006" have a file (EPUB), so `format:epub` matches exactly those six.
import { expect, test, type Page } from "@playwright/test";
import { api, book, grid, signIn, titles } from "./desktop-helpers";

test.describe.configure({ mode: "serial" });

const box = (page: Page) => page.getByPlaceholder("Filter by title or author");
const chips = (page: Page) => page.getByRole("group", { name: "Filters" });
const shown = async (page: Page) =>
  (await page.getByRole("gridcell").evaluateAll((els) => els.map((e) => e.getAttribute("aria-label")?.split(" by ")[0] ?? ""))).sort();
const SIX = titles(1, 6);

test.beforeEach(async ({ page }) => {
  await signIn(page);
  await expect(book(page, "Book 001")).toBeVisible();
});

test("the Filter menu adds an editable chip; the filter lives in the URL", async ({ page }) => {
  await page.getByRole("button", { name: "Filter", exact: true }).click();
  await page.getByRole("button", { name: /^Format/ }).click();
  await page.getByRole("checkbox", { name: "EPUB" }).check();
  await page.getByRole("button", { name: "Add filter" }).click();

  await expect(chips(page).getByRole("button", { name: "Format: EPUB. Edit" })).toBeVisible();
  await expect.poll(() => new URL(page.url()).searchParams.get("f")).toBe("format:epub");
  await expect.poll(() => shown(page)).toEqual(SIX);

  // The URL alone restores it.
  await page.reload();
  await expect.poll(() => shown(page)).toEqual(SIX);

  // Edit: "is not" flips it to the other 354.
  await chips(page).getByRole("button", { name: "Format: EPUB. Edit" }).click();
  await page.getByRole("button", { name: "is not", exact: true }).click();
  await page.getByRole("button", { name: "Apply" }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get("f")).toBe("-format:epub");
  await expect(grid(page)).toHaveAttribute("aria-rowcount", "59"); // 354 / 6 columns
  await expect(book(page, "Book 001")).toHaveCount(0);

  // × removes it: back to all 360.
  await chips(page).getByRole("button", { name: /^Remove Format/ }).click();
  await expect(grid(page)).toHaveAttribute("aria-rowcount", "60");
  expect(new URL(page.url()).searchParams.get("f")).toBeNull();
});

test("typed syntax turns into chips; Backspace takes the last one back", async ({ page }) => {
  await box(page).fill("format:epub ");
  await expect(chips(page).getByRole("button", { name: "Format: EPUB. Edit" })).toBeVisible();
  await expect(box(page)).toHaveValue("");
  // Free words still narrow by title and author, together with the chips; a finished token
  // among them becomes a chip and the words stay in the box.
  await box(page).fill('Book 00 author:"author 05" ');
  await expect(box(page)).toHaveValue("Book 00 ");
  await expect.poll(() => shown(page)).toEqual(["Book 005"]);
  await expect.poll(() => new URL(page.url()).searchParams.get("f")).toBe('format:epub author:"author 05"');

  await box(page).fill("");
  await box(page).press("Backspace");
  await expect(box(page)).toHaveValue('author:"author 05"');
  await expect(chips(page).getByRole("button", { name: /^Author/ })).toHaveCount(0);

  // A typo is explained, not silently dropped.
  await box(page).fill("year:abc");
  await box(page).press("Enter");
  await expect(page.locator("#lib-q-error")).toHaveText("Year: “abc” is not a whole number.");
  await expect(box(page)).toHaveAttribute("aria-describedby", "lib-q-error");
});

test("save as a smart shelf: it opens on its rule and refuses manual items", async ({ page }) => {
  await page.goto("/app/library?view=everything&f=format:epub");
  await expect.poll(() => shown(page)).toEqual(SIX);
  await page.getByRole("button", { name: "Save as smart shelf…" }).click();
  const dialog = page.getByRole("dialog", { name: "Save as smart shelf" });
  await dialog.getByLabel("Name").fill("E2E epubs");
  await dialog.getByRole("button", { name: "Save shelf" }).click();

  await expect.poll(() => new URL(page.url()).searchParams.get("shelf")).not.toBeNull();
  const id = Number(new URL(page.url()).searchParams.get("shelf"));
  await expect(page.getByText("Smart shelf: books where")).toBeVisible();
  await expect.poll(() => shown(page)).toEqual(SIX);

  const http = await api(page);
  const shelf = (await http.get("/api/shelves")).shelves.find((s: { id: number }) => s.id === id);
  expect(shelf).toMatchObject({ name: "E2E epubs", filter: "format:epub" });
  const me = await http.get("/api/me");
  const headers = { "x-csrf-token": me.csrf_token, origin: new URL(page.url()).origin };
  const add = await page.request.post(`/api/shelves/${id}/items`, { data: { work_id: 7 }, headers });
  expect(add.status()).toBe(409);
  expect((await add.json()).detail).toBe("smart_shelf");

  // Leave the other suites' top level as it was.
  expect((await page.request.delete(`/api/shelves/${id}`, { headers })).ok()).toBe(true);
});
