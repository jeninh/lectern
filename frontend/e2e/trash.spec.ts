// Multi-select on the Trash page: books and removed files in one selection.
import { expect, test } from "@playwright/test";
import { api, countText, idOf, signIn } from "./desktop-helpers";

test("select trashed books with click, Shift, Ctrl+A; restore and delete in bulk", async ({ page }) => {
  page.on("dialog", (d) => d.accept());
  await signIn(page, "/app/library");
  const a = await api(page);
  const ids = await Promise.all(["Book 200", "Book 201", "Book 202", "Book 203", "Book 204"].map((t) => idOf(page, t)));
  await a.post("/api/works/bulk", { work_ids: ids, action: "trash" });
  await page.goto("/app/trash");
  const item = (t: string) => page.getByRole("gridcell", { name: new RegExp(`^${t} by `) });
  await expect(page.getByRole("grid", { name: "Books" })).toHaveAttribute("aria-multiselectable", "true");
  // Trashed newest first; the bulk request trashed them in order, so Book 204 is on top.
  const order = await page.getByRole("gridcell").evaluateAll((els) => els.map((e) => e.getAttribute("aria-label")!.split(" by ")[0]));
  expect(order.slice(0, 5)).toEqual(["Book 204", "Book 203", "Book 202", "Book 201", "Book 200"]);

  await item("Book 204").click();
  await item("Book 202").click({ modifiers: ["Shift"] });
  await expect(countText(page)).toHaveText("3 selected");
  await page.keyboard.press("ArrowDown"); // focus 201, selection unchanged
  await page.keyboard.press("Space");
  await expect(countText(page)).toHaveText("4 selected");
  // Buttons inside a row keep their own clicks.
  await page.getByRole("button", { name: "Restore", exact: true }).last().click();
  await expect(page.locator(".toast").filter({ hasText: "Restored 4 items" })).toBeVisible();
  await expect(item("Book 204")).toHaveCount(0);
  await expect(item("Book 200")).toBeVisible();

  await item("Book 200").click();
  await page.keyboard.press("Control+a");
  await expect(countText(page)).toHaveText("All 1 selected");
  await page.getByRole("button", { name: "Delete now" }).last().click();
  await expect(page.locator(".toast").filter({ hasText: "Deleted 1 item" })).toBeVisible();
  await expect(page.getByText("Trash is empty")).toBeVisible();
});
