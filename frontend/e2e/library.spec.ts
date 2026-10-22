// Multi-select in the Library grid and list (lib/selection, lib/useSelection), against a
// seeded library of 360 books (e2e/seed.py): 6 columns at 1280×800, so 60 rows, of which only
// the ones near the viewport are in the DOM.
import { expect, test, type Page } from "@playwright/test";
import { api, book, countText, focusedLabel, grid, idOf, library, selectedTitles, signIn, titles } from "./desktop-helpers";

test.beforeEach(async ({ page }) => {
  await signIn(page);
  await expect(book(page, "Book 001")).toBeVisible();
});

const cols = async (page: Page) => Number(await grid(page).getAttribute("aria-colcount"));
const press = (page: Page, key: string) => page.keyboard.press(key);

test("ARIA grid with roving focus and windowed rows", async ({ page }) => {
  const g = grid(page);
  await expect(g).toHaveAttribute("aria-multiselectable", "true");
  expect(await cols(page)).toBe(6);
  await expect(g).toHaveAttribute("aria-rowcount", "60");
  const cells = page.getByRole("gridcell");
  const rendered = await cells.count();
  expect(rendered).toBeGreaterThan(12);
  expect(rendered).toBeLessThan(120); // windowed: far fewer than 360
  await expect(page.locator('[role="gridcell"][tabindex="0"]')).toHaveCount(1);
  await expect(cells.first()).toHaveAttribute("aria-selected", "false");
  // Tab reaches the grid on its one tabbable cell; the focus ring is drawn for the keyboard.
  await page.getByPlaceholder("Filter by title or author").focus();
  for (let i = 0; i < 12 && (await focusedLabel(page)) !== "Book 001"; i++) await press(page, "Tab");
  expect(await focusedLabel(page)).toBe("Book 001");
  const outline = await book(page, "Book 001").evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(outline).toBe("solid");
});

test("click selects one; Ctrl+click toggles; Shift+click ranges; Ctrl+Shift+click adds ranges", async ({ page }) => {
  await book(page, "Book 001").click();
  await expect(countText(page)).toHaveText("1 selected");
  await book(page, "Book 003").click();
  expect(await selectedTitles(page)).toEqual(["Book 003"]);

  await book(page, "Book 005").click({ modifiers: ["Control"] });
  await book(page, "Book 003").click({ modifiers: ["Control"] });
  expect(await selectedTitles(page)).toEqual(["Book 005"]);

  // Shift: anchor (Book 005, the last plain or Ctrl click) to the item, replacing the rest.
  await book(page, "Book 001").click();
  await book(page, "Book 004").click({ modifiers: ["Shift"] });
  expect(await selectedTitles(page)).toEqual(titles(1, 4));
  await book(page, "Book 002").click({ modifiers: ["Shift"] });
  expect(await selectedTitles(page)).toEqual(titles(1, 2));

  // Ctrl+Shift: the range is added to what was selected when the anchor was set.
  await book(page, "Book 009").click({ modifiers: ["Control"] }); // anchor 9; 1, 2, 9
  await book(page, "Book 011").click({ modifiers: ["Control", "Shift"] });
  expect(await selectedTitles(page)).toEqual([...titles(1, 2), ...titles(9, 11)]);
  // A second additive range from the same anchor replaces the first range only.
  await book(page, "Book 007").click({ modifiers: ["Control", "Shift"] });
  expect(await selectedTitles(page)).toEqual([...titles(1, 2), ...titles(7, 9)]);
  await expect(countText(page)).toHaveText("5 selected");

  // Esc clears.
  await press(page, "Escape");
  await expect(page.getByTestId("selection-bar")).toHaveCount(0);
  expect(await selectedTitles(page)).toEqual([]);
});

test("keyboard: 2D arrows, Shift+Arrow, Home/End, Ctrl+Arrow, Space, Enter, Esc", async ({ page }) => {
  await book(page, "Book 001").click(); // selects 1, focus 1
  await press(page, "ArrowRight");
  expect(await focusedLabel(page)).toBe("Book 002");
  await press(page, "ArrowDown"); // 6 columns: 2 → 8
  expect(await focusedLabel(page)).toBe("Book 008");
  expect(await selectedTitles(page)).toEqual(["Book 001"]); // arrows move focus only
  await press(page, "ArrowUp");
  expect(await focusedLabel(page)).toBe("Book 002");

  await press(page, "End"); // end of the row
  expect(await focusedLabel(page)).toBe("Book 006");
  await press(page, "Home");
  expect(await focusedLabel(page)).toBe("Book 001");
  await press(page, "Control+ArrowRight"); // Ctrl+Arrow jumps to the row's end
  expect(await focusedLabel(page)).toBe("Book 006");
  await press(page, "Control+ArrowLeft");
  expect(await focusedLabel(page)).toBe("Book 001");

  // Space toggles the focused item; Shift+Arrow extends from the anchor.
  await press(page, "ArrowDown"); // 7
  await press(page, "Space");
  expect(await selectedTitles(page)).toEqual(["Book 001", "Book 007"]);
  await press(page, "Shift+ArrowRight");
  await press(page, "Shift+ArrowRight");
  expect(await selectedTitles(page)).toEqual(["Book 001", ...titles(7, 9)]);
  await press(page, "Shift+ArrowLeft");
  expect(await selectedTitles(page)).toEqual(["Book 001", ...titles(7, 8)]);
  await press(page, "Shift+ArrowDown"); // 8 → 14: range 7..14
  expect(await selectedTitles(page)).toEqual(["Book 001", ...titles(7, 14)]);
  await press(page, "Shift+Home"); // row start of 14 is 13: range 7..13
  expect(await selectedTitles(page)).toEqual(["Book 001", ...titles(7, 13)]);
  await press(page, "Space"); // toggles 13 off
  expect(await selectedTitles(page)).toEqual(["Book 001", ...titles(7, 12)]);

  await press(page, "Escape");
  expect(await selectedTitles(page)).toEqual([]);
  // Enter opens the focused book.
  await press(page, "Enter");
  await expect(page.getByRole("dialog", { name: "Book 013" })).toBeVisible();
  await press(page, "Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  // Double-click opens too.
  await book(page, "Book 004").dblclick();
  await expect(page.getByRole("dialog", { name: "Book 004" })).toBeVisible();
});

test("virtualized: keyboard reaches unrendered books and Ctrl+A selects the whole view", async ({ page }) => {
  await expect(book(page, "Book 360")).toHaveCount(0); // not in the DOM yet
  await book(page, "Book 001").click();
  await press(page, "Control+End");
  await expect(book(page, "Book 360")).toBeFocused();
  await expect(book(page, "Book 001")).toHaveCount(0); // scrolled out and unrendered
  // Shift+Ctrl+Home from the bottom selects everything back to the top through unrendered rows.
  await press(page, "Shift+Control+Home");
  await expect(book(page, "Book 001")).toBeFocused();
  await expect(countText(page)).toHaveText("360 selected");
  await press(page, "Escape");

  // Walk down past the rendered window with the keyboard.
  for (let i = 0; i < 25; i++) await press(page, "ArrowDown");
  await expect(book(page, "Book 151")).toBeFocused();
  await expect(book(page, "Book 001")).toHaveCount(0);

  await press(page, "Control+a");
  await expect(countText(page)).toHaveText("All 360 selected");
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await expect(book(page, "Book 360")).toHaveAttribute("aria-selected", "true");
  await book(page, "Book 359").click({ modifiers: ["Control"] });
  await expect(countText(page)).toHaveText("359 selected");
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect(book(page, "Book 001")).toHaveAttribute("aria-selected", "true");
});

test("virtualized: focus survives its row scrolling away, and the grid keeps a tab stop", async ({ page }) => {
  // Scrolled away without focusing the grid: the first rendered cell is the tab stop.
  await page.mouse.wheel(0, 6000);
  await expect(book(page, "Book 001")).toHaveCount(0);
  await expect(page.locator('[role="gridcell"][tabindex="0"]')).toHaveCount(1);
  await page.evaluate(() => window.scrollTo(0, 0));

  await book(page, "Book 001").click();
  await page.mouse.wheel(0, 6000);
  await expect(book(page, "Book 001")).toHaveCount(0);
  // Focus stays on the grid (not <body>), so keys still drive the selection.
  expect(await page.evaluate(() => document.activeElement?.getAttribute("role"))).toBe("grid");
  await page.keyboard.press("ArrowDown");
  await expect(book(page, "Book 007")).toBeFocused();
  await page.keyboard.press("Shift+ArrowRight");
  expect(await selectedTitles(page)).toEqual(["Book 001", "Book 007", "Book 008"]);
});

test("list view: one column, arrows by one, Shift+End to the end", async ({ page }) => {
  await page.getByRole("button", { name: "List" }).click();
  expect(await cols(page)).toBe(1);
  await expect(grid(page)).toHaveAttribute("aria-rowcount", "360");
  expect(await page.getByRole("gridcell").count()).toBeLessThan(80);
  await book(page, "Book 001").click();
  await press(page, "ArrowDown");
  expect(await focusedLabel(page)).toBe("Book 002");
  await press(page, "Shift+End");
  await expect(book(page, "Book 360")).toBeFocused();
  // Arrows moved focus only, so Book 001 stays selected and 2..360 is added.
  await expect(countText(page)).toHaveText("360 selected");
  await press(page, "Control+a");
  await expect(countText(page)).toHaveText("All 360 selected");
  await page.getByRole("button", { name: "Grid" }).click();
});

test("filtered view: Ctrl+A sends a server-side selection for books never loaded on screen", async ({ page }) => {
  test.setTimeout(120_000); // 60 per-work transactions on the server
  await page.getByPlaceholder("Filter by title or author").fill("Book 3");
  await expect(page.getByText("61 of 360 books shown")).toBeAttached();
  await book(page, "Book 300").click();
  await press(page, "Control+a");
  await expect(countText(page)).toHaveText("All 61 selected");
  await book(page, "Book 301").click({ modifiers: ["Control"] });
  await expect(countText(page)).toHaveText("60 selected");
  await expect(book(page, "Book 360")).toHaveCount(0);

  await page.getByRole("button", { name: "Tags", exact: true }).click();
  await page.locator("#bulk-tags").fill("e2e-all");
  await press(page, "Enter");
  const req = page.waitForRequest((r) => r.url().endsWith("/api/works/bulk") && r.method() === "POST");
  await page.getByRole("button", { name: "Add these tags" }).click();
  const sent = await req;
  const body = sent.postDataJSON();
  const res = await sent.response(); // waits for all 60 works
  expect(res?.status()).toBe(200);
  const excluded = await idOf(page, "Book 301");
  expect(body.selection).toEqual({ filter: { q: "Book 3", status: "all", shelf: null }, exclude_ids: [excluded], view_token: expect.stringMatching(/^[0-9a-f]{32}$/), expected_count: 60 });
  expect(body.work_ids).toBeUndefined();
  await expect(page.locator(".toast").filter({ hasText: "Tagged" })).toBeVisible();

  const tagged = (await library(page)).filter((w) => w.tags.includes("e2e-all")).map((w) => w.title).sort();
  expect(tagged).toHaveLength(60);
  expect(tagged).not.toContain("Book 301");
  expect(tagged).toContain("Book 360");
});

test("search results: Shift ranges follow the filtered order", async ({ page }) => {
  await page.getByPlaceholder("Filter by title or author").fill("Author 05");
  await expect(book(page, "Book 005")).toBeVisible();
  await book(page, "Book 005").click();
  await book(page, "Book 029").click({ modifiers: ["Shift"] });
  expect(await selectedTitles(page)).toEqual(["Book 005", "Book 017", "Book 029"]);
  // A different view starts a fresh selection.
  await page.getByPlaceholder("Filter by title or author").fill("Author 06");
  await expect(page.getByTestId("selection-bar")).toHaveCount(0);
});

test("shelves: add the selection to a shelf, then select all on that shelf", async ({ page }) => {
  const a = await api(page);
  const name = `E2E shelf ${Date.now() % 100000}`;
  await a.post("/api/shelves", { name });
  await page.reload();
  await page.getByPlaceholder("Filter by title or author").fill("Book 04");
  await book(page, "Book 040").click();
  await book(page, "Book 042").click({ modifiers: ["Shift"] });
  await page.getByRole("button", { name: "Shelves", exact: true }).last().click();
  await page.getByRole("dialog").getByRole("button", { name }).click();
  await page.getByRole("button", { name: "Add to shelves" }).click();
  await expect(page.locator(".toast").filter({ hasText: "Shelved" })).toBeVisible();
  await page.getByPlaceholder("Filter by title or author").fill("");
  await page.getByRole("group", { name: "Shelves" }).getByRole("button", { name: new RegExp(name) }).click();
  const g = page.getByRole("grid", { name: `Books on ${name}` });
  await expect(g.getByRole("gridcell")).toHaveCount(3);
  await book(page, "Book 041").click();
  await press(page, "Control+a");
  await expect(countText(page)).toHaveText("All 3 selected");
});

test("selection bar: send to a device and align (after a confirmation)", async ({ page }) => {
  const a = await api(page);
  await a.post("/api/devices", { label: "E2E Kobo" });
  await book(page, "Book 001").click();
  await book(page, "Book 003").click({ modifiers: ["Shift"] });
  await page.getByRole("button", { name: "Send", exact: true }).click();
  const send = page.getByRole("dialog", { name: "Send 3 books to a device" });
  await send.getByRole("button", { name: "Send 3 books" }).click();
  await expect(page.locator(".toast").filter({ hasText: "Sent 3 books to E2E Kobo" })).toBeVisible();

  await book(page, "Book 001").click();
  await book(page, "Book 002").click({ modifiers: ["Shift"] });
  // Declining the confirmation sends nothing.
  let asked = "";
  let sent = 0;
  page.on("request", (r) => r.url().endsWith("/api/works/align") && sent++);
  page.once("dialog", (d) => ((asked = d.message()), d.dismiss()));
  await page.getByRole("button", { name: "Align" }).click();
  await expect.poll(() => asked).toContain("Queue alignment for 2 books?");
  await page.waitForTimeout(300);
  expect(sent).toBe(0);
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Align" }).click();
  await expect(page.locator(".toast").filter({ hasText: "2 books without both an EPUB and audio" })).toBeVisible();
  expect(sent).toBe(1);
  // Over the cap for alignment (50), Align is off; Ctrl+A over 360 books.
  await page.keyboard.press("Control+a");
  await expect(countText(page)).toHaveText("All 360 selected");
  await expect(page.getByRole("button", { name: "Align" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeDisabled();
});

test("selection bar: trash, then Undo", async ({ page }) => {
  page.on("dialog", (d) => d.accept());
  await page.getByPlaceholder("Filter by title or author").fill("Book 35");
  await book(page, "Book 350").click();
  await book(page, "Book 352").click({ modifiers: ["Shift"] });
  await page.getByRole("button", { name: "Trash" }).click();
  await expect(page.locator(".toast").filter({ hasText: "Moved to trash" })).toBeVisible();
  await expect(book(page, "Book 351")).toHaveCount(0);
  await expect(page.getByTestId("selection-bar")).toHaveCount(0);
  await page.locator(".toast").getByRole("button", { name: "Undo" }).click();
  await expect(book(page, "Book 351")).toBeVisible();
});

test("marquee: drag across empty space to select a rectangle", async ({ page }) => {
  const first = (await book(page, "Book 001").boundingBox())!;
  const row2 = (await book(page, "Book 009").boundingBox())!;
  // Start in the gaps between columns 1 and 2 and rows 1 and 2; end inside Book 009.
  const x0 = first.x + first.width + 8;
  const y0 = first.y + first.height + 8;
  expect(y0).toBeLessThan(row2.y);
  await page.mouse.move(x0, y0);
  await page.mouse.down();
  await page.mouse.move(row2.x + row2.width / 2, row2.y + 40, { steps: 6 });
  await expect(page.locator(".marquee")).toBeVisible();
  await page.mouse.up();
  expect(await selectedTitles(page)).toEqual(titles(8, 9));
  // A plain click on empty space clears.
  await page.mouse.click(x0, y0);
  expect(await selectedTitles(page)).toEqual([]);
});

test("never hijacks inputs, Ctrl+K, or the reader", async ({ page }) => {
  const filter = page.getByPlaceholder("Filter by title or author");
  await book(page, "Book 001").click();
  await filter.fill("Book");
  await filter.press("Control+a"); // selects the text, not the books
  expect(await filter.evaluate((el: HTMLInputElement) => [el.selectionStart, el.selectionEnd])).toEqual([0, 4]);
  await expect(page.getByTestId("selection-bar")).toHaveCount(0); // the new view cleared it
  await filter.press("Escape"); // clears the filter, as before
  await expect(filter).toHaveValue("");

  await book(page, "Book 002").click();
  await press(page, "Control+k");
  const palette = page.getByRole("dialog", { name: "Search and commands" });
  await expect(palette).toBeVisible();
  await page.keyboard.type("Book 00");
  await page.keyboard.press("Control+a"); // the palette's own field
  await expect(countText(page)).toHaveText("1 selected");
  await press(page, "Escape");
  await expect(palette).toHaveCount(0);
  await expect(countText(page)).toHaveText("1 selected"); // Esc closed the palette only

  // Detail sheet open: arrows and Ctrl+A are the sheet's, Esc closes it.
  await press(page, "Enter");
  await expect(page.getByRole("dialog", { name: "Book 002" })).toBeVisible();
  await press(page, "Control+a");
  await expect(countText(page)).toHaveText("1 selected");
  await press(page, "Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);

  // The reader: no collection, so nothing is taken.
  await page.goto(`/app/reader/${await idOf(page, "Book 001")}`);
  const prevented = await page.evaluate(() => {
    const ev = new KeyboardEvent("keydown", { key: "a", ctrlKey: true, bubbles: true, cancelable: true });
    document.body.dispatchEvent(ev);
    return ev.defaultPrevented;
  });
  expect(prevented).toBe(false);
  await expect(page.getByTestId("selection-bar")).toHaveCount(0);
});
