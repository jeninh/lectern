// The Docs tab (pages/Docs.tsx): docs/*.md rendered in the app, linked between, and searchable.
import { expect, test } from "@playwright/test";
import { signIn } from "./desktop-helpers";

test("browse a doc, follow an in-app link, and find another from the palette", async ({ page }) => {
  await signIn(page, "/app");
  await page.getByRole("complementary", { name: "Sidebar" }).getByRole("link", { name: /^Docs/ }).click();
  await expect(page).toHaveURL(/\/app\/docs$/);
  await page.locator(".docs-overview").getByRole("link", { name: /Raspberry Pi setup/ }).click();

  const doc = page.getByRole("article", { name: "Raspberry Pi setup" });
  await expect(page.getByRole("heading", { level: 1, name: "Raspberry Pi setup" })).toBeVisible();
  await expect(doc.getByRole("navigation", { name: "Breadcrumb" })).toContainText("Getting started");
  // A heading has GitHub's anchor; a code block has a copy button; the next page is linked.
  await expect(doc.locator('h2[id="4-updating"]')).toBeVisible();
  await expect(doc.getByRole("button", { name: "Copy code" }).first()).toBeVisible();
  await expect(doc.getByRole("navigation", { name: "Previous and next" }).getByRole("link", { name: /Tailscale/ })).toBeVisible();

  // An in-page link moves to its heading; a link to another doc stays in the app.
  await doc.getByRole("link", { name: "step 4", exact: true }).first().click();
  await expect(page).toHaveURL(/\/app\/docs\/pi-setup#4-updating$/);
  await page.goto("/app/docs/deploy-tailscale");
  await page.getByRole("article").getByRole("link", { name: "end-to-end Pi setup guide" }).click();
  await expect(page).toHaveURL(/\/app\/docs\/pi-setup$/);

  // Cmd+K "Docs: backups" opens the doc.
  await page.keyboard.press("Control+k");
  await page.getByRole("combobox").fill("docs: backups");
  await page.getByRole("option", { name: /Docs: Backups and restore/ }).click();
  await expect(page).toHaveURL(/\/app\/docs\/backups$/);
  // Screenshots load from the build, for signed-in users.
  const shot = page.getByRole("article").locator("img.doc-img").first();
  await shot.scrollIntoViewIfNeeded();
  await expect.poll(() => shot.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth)).toBeGreaterThan(0);
});

test("search the docs from the page", async ({ page }) => {
  await signIn(page, "/app/docs");
  await page.getByRole("searchbox", { name: "Search docs" }).fill("restore");
  const results = page.getByRole("region", { name: "Search results" });
  await expect(results.getByRole("link").first()).toContainText("Restore");
  await results.getByRole("link").first().click();
  await expect(page).toHaveURL(/\/app\/docs\/backups#restore$/);
});

test("screenshots need a session", async ({ browser }) => {
  const ctx = await browser.newContext();
  const r = await ctx.request.get("/app/doc-media/backups-admin.png", { maxRedirects: 0 });
  expect([303, 401]).toContain(r.status());
  await ctx.close();
});
