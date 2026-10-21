// Positions saved offline: synced on reconnect, a prompt when another device saved later,
// revoked books deleted on reconnect, and everything cleared on sign-out.
import { expect, test } from "@playwright/test";
import { apiAs, cacheKeys, controlled, downloadBook, MAGI, shot, signIn, userOf, workId } from "./helpers";

test.describe.configure({ mode: "serial" });

test("offline page turns sync on reconnect; a later save elsewhere asks which to keep", async ({ page, context, baseURL }, info) => {
  const user = userOf(info);
  await signIn(page, user);
  await controlled(page);
  const pdfWork = await workId(page, "Field Notes");
  await downloadBook(page, pdfWork);
  const fileId = await page.evaluate(async (id) => (await (await fetch(`/api/works/${id}`)).json()).files[0].id as number, pdfWork);

  // Start from page 1 even when a reused local server kept a previous run's position.
  await (await apiAs(baseURL!, user)).put(`/api/files/${fileId}/progress`, { page: 1, pages: 6, device: "Kobo Libra" });

  // 1) No conflict: page 3 recorded offline syncs silently on reconnect.
  await page.goto(`/app/reader/${pdfWork}?file=${fileId}`);
  await expect(page.getByText("PDF · Page 1 of 6")).toBeVisible({ timeout: 20_000 });
  await context.setOffline(true);
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowRight");
  await expect(page.locator(".net-status")).toHaveAttribute("aria-label", /1 reading position waits to sync/, { timeout: 10_000 });
  await context.setOffline(false);
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  const other = await apiAs(baseURL!, user, "Kobo Libra");
  await expect.poll(async () => (await (await other.api.get(`/api/files/${fileId}/pages`)).json()).progress?.page, { timeout: 20_000 }).toBe(3);

  // 2) Conflict: this device reads offline to page 5, then another device saves page 2.
  await context.setOffline(true);
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowRight");
  await expect(page.locator(".net-status")).toHaveAttribute("aria-label", /1 reading position waits/, { timeout: 10_000 });
  await page.waitForTimeout(1100); // the other device's save is strictly later
  await other.put(`/api/files/${fileId}/progress`, { page: 2, pages: 6, device: "Kobo Libra" });
  await context.setOffline(false);
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  const prompt = page.getByTestId("conflict");
  await expect(prompt).toBeVisible({ timeout: 20_000 });
  await expect(prompt).toContainText("page 5");
  await expect(prompt).toContainText("Kobo Libra saved page 2");
  await shot(page, info, "conflict");
  await prompt.getByRole("button", { name: "Keep this device’s" }).click();
  await expect(prompt).toBeHidden();
  await expect.poll(async () => (await (await other.api.get(`/api/files/${fileId}/pages`)).json()).progress?.page).toBe(5);

  // 3) Conflict answered the other way: the viewer moves to the other device's page.
  await context.setOffline(true);
  await page.keyboard.press("ArrowLeft");
  await expect(page.locator(".net-status")).toHaveAttribute("aria-label", /1 reading position waits/, { timeout: 10_000 });
  await page.waitForTimeout(1100);
  await other.put(`/api/files/${fileId}/progress`, { page: 6, pages: 6, device: "Kobo Libra" });
  await context.setOffline(false);
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await expect(prompt).toBeVisible({ timeout: 20_000 });
  await prompt.getByRole("button", { name: /Use Kobo Libra/ }).click();
  await expect(prompt).toBeHidden();
  await expect(page.getByText("PDF · Page 6 of 6")).toBeVisible();
  expect((await (await other.api.get(`/api/files/${fileId}/pages`)).json()).progress.page).toBe(6);
  await other.api.dispose();
});

test("a book no longer shared is deleted from the device on reconnect", async ({ page, context, baseURL }, info) => {
  const user = userOf(info);
  await signIn(page, user);
  await controlled(page);
  const pdfWork = await workId(page, "Field Notes");
  if (!(await page.evaluate(async () => (await caches.keys()).includes("lectern-books")))) await downloadBook(page, pdfWork);
  await page.goto("/app/downloads");
  await expect(page.getByTestId(`downloaded-${pdfWork}`)).toBeVisible();

  await context.setOffline(true);
  await page.reload();
  await expect(page.locator(".net-status")).toHaveText("Offline");
  // While offline, the owner takes the book off this reader's shelf.
  const owner = await apiAs(baseURL!, "owner");
  const shelves = (await (await owner.api.get("/api/shelves")).json()).shelves as { id: number; name: string }[];
  const shelf = shelves.find((s) => s.name === `For ${user}`)!;
  expect(await owner.del(`/api/shelves/${shelf.id}/items/${pdfWork}`)).toBe(204);

  await context.setOffline(false);
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await expect(page.locator(".toast").filter({ hasText: "no longer available to you: Field Notes" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId(`downloaded-${pdfWork}`)).toHaveCount(0);
  const keys = (await cacheKeys(page))["lectern-books"] ?? [];
  expect(keys.filter((k) => k.includes(`/works/${pdfWork}/`) || k.includes("/view"))).toEqual([]);
  // Put it back for the other project and later runs.
  await owner.api.post(`/api/shelves/${shelf.id}/items`, { data: { work_id: pdfWork }, headers: { "x-csrf-token": owner.csrf } });
  await owner.api.dispose();
});

test("signing out deletes downloads, read-along data, queued positions, and caches", async ({ page }, info) => {
  await signIn(page, userOf(info));
  await controlled(page);
  const id = await workId(page, MAGI);
  await downloadBook(page, id);
  expect((await cacheKeys(page))["lectern-books"]?.length).toBeGreaterThan(0);
  await page.goto("/app/security");
  await page.getByRole("button", { name: "Sign out", exact: true }).first().click();
  await page.waitForURL(/\/login/);
  const left = await page.evaluate(async () => ({
    caches: await caches.keys(),
    dbs: (await indexedDB.databases?.())?.map((d) => d.name) ?? [],
    marks: Object.keys(localStorage).filter((k) => /^lectern\.(read|listen|page)\./.test(k)),
    workers: (await navigator.serviceWorker.getRegistrations()).length,
  }));
  expect(left.caches.filter((c) => c.startsWith("lectern-"))).toEqual([]);
  expect(left.dbs).not.toContain("lectern-offline");
  expect(left.marks).toEqual([]);
  expect(left.workers).toBe(0);
});
