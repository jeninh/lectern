// The Library as a file manager (components/Folders, lib/folders, lib/shelves): shelf tiles
// before books, folders with breadcrumbs, history, and URLs, the sidebar tree, drag and drop
// (Alt adds), Move to… on a mixed selection, deleting a non-empty shelf, and Ctrl+K paths.
// Uses Books 201–215 of e2e/seed.py and shelves named "F …", and puts everything back.
import { expect, test, type Page } from "@playwright/test";
import { api, book, circle, countText, idOf, signIn } from "./desktop-helpers";


const tile = (page: Page, name: string) => page.getByRole("gridcell", { name: new RegExp(`^Shelf ${name},`) });
const filter = (page: Page) => page.getByRole("search", { name: "Library" }).getByRole("textbox");
const crumbs = (page: Page) => page.getByRole("navigation", { name: "Folder" });
const shelfIds = async (page: Page, title: string): Promise<number[]> =>
  ((await (await page.request.get("/api/library")).json()).works as { title: string; shelf_ids: number[] }[]).find((w) => w.title === title)!.shelf_ids;
const shelfByName = async (page: Page, name: string) =>
  ((await (await page.request.get("/api/shelves")).json()).shelves as { id: number; name: string; parent_id: number | null }[]).find((s) => s.name === name)!;

let ids: Record<string, number> = {};

test.beforeAll(async ({ browser }) => {
  const page = await browser.newPage();
  await signIn(page, "/app");
  const a = await api(page);
  // A worker restarted after a failure runs this again: start from no shelves each time.
  const me = await (await page.request.get("/api/me")).json();
  const headers = { "x-csrf-token": me.csrf_token, origin: new URL(page.url()).origin };
  for (const s of (await a.get("/api/shelves")).shelves.reverse()) await page.request.delete(`/api/shelves/${s.id}?contents=move_up`, { headers });
  for (const t of ["Book 201", "Book 202", "Book 203", "Book 204", "Book 205"]) expect(await shelfIds(page, t)).toEqual([]);
  const kids = (await a.post("/api/shelves", { name: "F Kids" })).id;
  const picture = (await a.post("/api/shelves", { name: "F Picture", parent_id: kids })).id;
  const other = (await a.post("/api/shelves", { name: "F Other" })).id;
  await a.post(`/api/shelves/${kids}/items`, { work_id: await idOf(page, "Book 201") });
  await a.post(`/api/shelves/${picture}/items`, { work_id: await idOf(page, "Book 202") });
  await a.post(`/api/shelves/${other}/items`, { work_id: await idOf(page, "Book 205") });
  ids = { kids, picture, other };
  await page.close();
});

test.afterAll(async ({ browser }) => {
  // Leave the seeded library as the other suites expect it: no shelves, every book at the root.
  const page = await browser.newPage();
  await signIn(page, "/app");
  const me = await (await page.request.get("/api/me")).json();
  const headers = { "x-csrf-token": me.csrf_token, origin: new URL(page.url()).origin };
  for (const s of (await (await page.request.get("/api/shelves")).json()).shelves.reverse()) {
    await page.request.delete(`/api/shelves/${s.id}?contents=move_up`, { headers });
  }
  await page.close();
});

test.beforeEach(async ({ page }) => {
  await signIn(page, "/app/library");
  await expect(tile(page, "F Kids")).toBeVisible();
});

test("Folders: shelves first, books on no shelf, open, breadcrumbs, Back, and the URL", async ({ page }) => {
  const root = page.getByRole("grid", { name: "Shelves and books not on a shelf" });
  // Shelves come before books; a book on a shelf is not at the root.
  const first = await root.getByRole("gridcell").first().getAttribute("aria-label");
  expect(first).toMatch(/^Shelf F /);
  // The filter narrows the books here (and shelf names): Book 201 is on a shelf, 203 is not.
  await filter(page).fill("Book 20");
  await expect(book(page, "Book 203")).toBeVisible();
  await expect(book(page, "Book 201")).toHaveCount(0);
  await expect(tile(page, "F Kids")).toHaveCount(0);
  await filter(page).fill("");

  // Not selecting: a plain click opens a shelf.
  await tile(page, "F Kids").click();
  await expect(page).toHaveURL(new RegExp(`/app/library\\?shelf=${ids.kids}$`));
  await expect(crumbs(page)).toHaveText(/Library›F Kids/);
  await expect(tile(page, "F Picture")).toBeVisible();
  await expect(book(page, "Book 201")).toBeVisible();
  await expect(page.getByRole("grid")).toHaveAttribute("aria-label", "Shelves and books in F Kids");

  // Selecting: a click toggles a shelf instead; Enter on the focused shelf still opens it.
  await circle(book(page, "Book 201"));
  await tile(page, "F Picture").click();
  await expect(countText(page)).toHaveText("2 selected");
  await expect(page).toHaveURL(new RegExp(`shelf=${ids.kids}$`));
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`shelf=${ids.picture}`));
  await expect(crumbs(page)).toHaveText(/Library›F Kids›F Picture/);
  await expect(book(page, "Book 202")).toBeVisible();

  // History: Back returns to the parent; a breadcrumb opens the root.
  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`shelf=${ids.kids}$`));
  await crumbs(page).getByRole("link", { name: "Library" }).click();
  await expect(tile(page, "F Kids")).toBeVisible();

  // The sidebar tree: Kids opened itself to show where Picture is; it collapses and expands.
  const tree = page.getByRole("tree", { name: "Shelves" });
  await tree.getByRole("button", { name: "Collapse F Kids" }).click();
  await expect(tree.getByRole("link", { name: "F Picture" })).toHaveCount(0);
  await tree.getByRole("button", { name: "Expand F Kids" }).click();
  await tree.getByRole("link", { name: "F Picture" }).click();
  await expect(page).toHaveURL(new RegExp(`shelf=${ids.picture}`));
  await expect(tree.getByRole("link", { name: "F Picture" })).toHaveAttribute("aria-current", "page");

  // Everything is the flat view of every book, shelved or not.
  await page.getByRole("button", { name: "Everything" }).click();
  await expect(page).toHaveURL(/view=everything/);
  await expect(book(page, "Book 001")).toBeVisible();
  await expect(page.getByRole("grid", { name: "Every book" }).getByRole("gridcell", { name: /^Shelf / })).toHaveCount(0);
});

test("New shelf is made in the folder you are in", async ({ page }) => {
  await page.goto(`/app/library?shelf=${ids.kids}`);
  await page.getByRole("button", { name: "New shelf" }).click();
  const dialog = page.getByRole("dialog", { name: "New shelf in F Kids" });
  await dialog.getByRole("textbox", { name: "Name" }).fill("F Comics");
  await dialog.getByRole("button", { name: "Create" }).click();
  await expect(tile(page, "F Comics")).toBeVisible();
  expect((await shelfByName(page, "F Comics")).parent_id).toBe(ids.kids);
});

test("drag and drop: a plain drop moves, Alt adds, a breadcrumb takes drops too", async ({ page }) => {
  // Root → F Kids in the sidebar tree: on Kids, and no longer at the root.
  await filter(page).fill("Book 203");
  const tree = page.getByRole("tree", { name: "Shelves" });
  await book(page, "Book 203").dragTo(tree.getByRole("link", { name: "F Kids" }));
  await expect(page.locator(".toast").filter({ hasText: "Moved 1 book to F Kids" })).toBeVisible();
  await expect(book(page, "Book 203")).toHaveCount(0);
  expect(await shelfIds(page, "Book 203")).toEqual([ids.kids]);

  // Inside Kids, Alt-drop onto the Picture tile adds: on both now (several shelves).
  await page.goto(`/app/library?shelf=${ids.kids}`);
  await page.keyboard.down("Alt");
  await book(page, "Book 203").dragTo(tile(page, "F Picture"));
  await page.keyboard.up("Alt");
  await expect(page.locator(".toast").filter({ hasText: "Added 1 book to F Picture" })).toBeVisible();
  await expect(book(page, "Book 203")).toBeVisible();
  expect((await shelfIds(page, "Book 203")).sort()).toEqual([ids.kids, ids.picture].sort());

  // Drop on the "Library" breadcrumb: off this shelf (it stays on Picture).
  await book(page, "Book 203").dragTo(crumbs(page).getByRole("link", { name: "Library" }));
  await expect(book(page, "Book 203")).toHaveCount(0);
  expect(await shelfIds(page, "Book 203")).toEqual([]);
  // Undo puts it back on Kids.
  await page.locator(".toast").last().getByRole("button", { name: "Undo" }).click();
  await expect(book(page, "Book 203")).toBeVisible();
  expect((await shelfIds(page, "Book 203")).sort()).toEqual([ids.kids, ids.picture].sort());
});

test("Move to…: a shelf and a book together, never into itself; Ctrl+A counts both", async ({ page }) => {
  await page.goto(`/app/library?shelf=${ids.kids}`);
  await circle(tile(page, "F Comics"));
  await book(page, "Book 201").click({ modifiers: ["Control"] });
  await expect(countText(page)).toHaveText("2 selected");
  // Book-only actions are off while a shelf is selected.
  await expect(page.getByRole("button", { name: "Tags", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Move to…" }).click();
  const picker = page.getByRole("dialog", { name: "Move or add 1 shelf and 1 book" });
  await expect(picker.getByRole("option", { name: /^F Comics/ })).toHaveAttribute("aria-disabled", "true"); // itself
  await expect(picker.getByRole("option", { name: /^F Kids/ })).toHaveAttribute("aria-disabled", "true"); // already here
  await expect(picker.getByRole("button", { name: "Add to shelf" })).toHaveCount(0); // shelves only move
  await picker.getByRole("option", { name: /^F Other/ }).click();
  await picker.getByRole("button", { name: "Move here" }).click();
  await expect(page.locator(".toast").filter({ hasText: "Moved 1 shelf and 1 book to F Other" })).toBeVisible();
  await expect(tile(page, "F Comics")).toHaveCount(0);
  expect((await shelfByName(page, "F Comics")).parent_id).toBe(ids.other);
  expect(await shelfIds(page, "Book 201")).toEqual([ids.other]);

  // Kids can't go into Picture, which is inside it.
  await page.goto("/app/library");
  await circle(tile(page, "F Kids"));
  await page.getByRole("button", { name: "Move to…" }).click();
  await expect(page.getByRole("dialog").getByRole("option", { name: /^F Picture/ })).toHaveAttribute("aria-disabled", "true");
  await page.keyboard.press("Escape");

  // Select all in Kids: its shelves and its books.
  await page.goto(`/app/library?shelf=${ids.kids}`);
  const cells = page.getByRole("grid").getByRole("gridcell");
  await expect(tile(page, "F Picture")).toBeVisible();
  const n = await cells.count();
  await cells.first().focus();
  await page.keyboard.press("Control+a");
  await expect(countText(page)).toHaveText(`All ${n} selected`);
});

test("deleting a shelf with things in it: its contents move up", async ({ page }) => {
  // F Other holds Book 205, Book 201, and F Comics; it is top-level, so they go to the root.
  await tile(page, "F Other").hover();
  await page.getByRole("button", { name: "More for F Other" }).click();
  await page.getByRole("menuitem", { name: "Delete…" }).click();
  const dialog = page.getByRole("dialog", { name: "Delete “F Other”?" });
  await expect(dialog).toContainText("It holds 2 books and 1 sub-shelf");
  await expect(dialog.getByRole("radio", { name: /Move its contents up to the top level/ })).toBeChecked();
  await expect(dialog.getByRole("radio", { name: /Move its 2 books to the Trash/ })).toBeVisible();
  await dialog.getByRole("button", { name: "Delete shelf" }).click();
  await expect(tile(page, "F Other")).toHaveCount(0);
  await expect(tile(page, "F Comics")).toBeVisible(); // now top-level
  expect(await shelfIds(page, "Book 205")).toEqual([]);
  await filter(page).fill("Book 205");
  await expect(book(page, "Book 205")).toBeVisible(); // at the root
});

test("Ctrl+K opens a shelf by its path", async ({ page }) => {
  await page.keyboard.press("Control+k");
  await page.keyboard.type("f kids/f pic");
  const option = page.getByRole("option", { name: /F Kids \/ F Picture/ });
  await expect(option).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`shelf=${ids.picture}`));
  await expect(crumbs(page)).toHaveText(/F Kids›F Picture/);
});

test("Auto-shelves: an Authors group shows in the tree and the folder; its shelves cannot be renamed or deleted", async ({ page }) => {
  await page.getByRole("button", { name: "Add a shelf or auto-shelves" }).click();
  await page.getByRole("menuitem", { name: "Auto-shelves…" }).click();
  const dialog = page.getByRole("dialog", { name: "New auto-shelves" });
  await expect(dialog.getByRole("textbox", { name: "Name" })).toHaveValue("Authors");
  await dialog.getByRole("button", { name: "Create" }).click();
  // The new group opens: A–Z shelves (the seed's "Author 00"…), none with Rename, Move, or Delete.
  await expect(crumbs(page)).toHaveText(/Library›Authors/);
  const bucket = page.getByRole("gridcell", { name: /^Auto-shelf A,/ });
  await expect(bucket).toBeVisible();
  await bucket.hover();
  await page.getByRole("button", { name: "More for A", exact: true }).click();
  await expect(page.getByRole("menuitem", { name: "Open" })).toBeVisible();
  for (const name of ["Rename…", "Move to…", "Sharing…", "Delete…"]) await expect(page.getByRole("menuitem", { name })).toHaveCount(0);
  await page.keyboard.press("Escape");
  // In the sidebar tree the group is collapsed until something inside it is open.
  await page.goto("/app/library");
  const tree = page.getByRole("tree", { name: "Shelves" });
  await expect(tree.getByRole("treeitem", { name: /Authors/ }).first()).toHaveAttribute("aria-expanded", "false");
  // The group itself has its settings, and is deleted without a question about contents.
  await page.getByRole("gridcell", { name: /^Auto-shelf Authors,/ }).hover();
  await page.getByRole("button", { name: "More for Authors", exact: true }).click();
  await expect(page.getByRole("menuitem", { name: "Auto-shelf settings…" })).toBeVisible();
  await page.getByRole("menuitem", { name: "Delete…" }).click();
  await page.getByRole("dialog").getByRole("button", { name: /^Delete/ }).click();
  await expect(page.getByRole("gridcell", { name: /^Auto-shelf Authors,/ })).toHaveCount(0);
});
