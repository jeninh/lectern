// The Docs tab (pages/Docs.tsx): docs/*.md rendered in the app, linked between, and searchable.
import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parse } from "yaml";
import type { Manifest } from "../src/lib/docs";
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

test("a reader's browser never receives the docs hidden from readers", async ({ page }) => {
  // Every same-origin response the browser gets while a reader opens the Docs tab and the palette.
  const bodies: Promise<string>[] = [];
  const base = test.info().project.use.baseURL!;
  page.on("response", (r) => {
    if (r.url().startsWith(base) && r.request().resourceType() !== "image") bodies.push(r.text().catch(() => ""));
  });
  await signIn(page, "/app/docs", "reader");
  const overview = page.locator(".docs-overview");
  await expect(overview.getByRole("link", { name: /Read-along/ }).first()).toBeVisible();
  await expect(overview.getByRole("link", { name: /Raspberry Pi setup|Backups and restore|Contracts/ })).toHaveCount(0);
  await page.goto("/app/docs/backups");
  await expect(page.getByRole("heading", { name: "No such page" })).toBeVisible();
  await page.keyboard.press("Control+k");
  await page.getByRole("combobox").fill("docs: restore");
  await expect(page.getByRole("option", { name: /Backups and restore/ })).toHaveCount(0);
  // Read straight from the server too, as a script in the reader's session would.
  const direct = await page.request.get("/app/doc-text");
  expect(direct.status()).toBe(200);
  bodies.push(direct.text());

  const received = (await Promise.all(bodies)).join("\n");
  const docs = fileURLToPath(new URL("../../docs", import.meta.url));
  const manifest = parse(readFileSync(resolve(docs, "index.yml"), "utf8")) as Manifest;
  const all = manifest.sections.flatMap((s) => s.docs);
  // A line long enough to be the doc's own text (JSON escaping aside), from each doc.
  const lineOf = (file: string) => readFileSync(resolve(docs, file), "utf8").split("\n").map((l) => l.trim()).filter((l) => /^[\w ,.'()-]{60,}$/.test(l))[0];
  const shown = all.filter((d) => (d.audience ?? "everyone") === "everyone");
  const hidden = all.filter((d) => (d.audience ?? "everyone") !== "everyone");
  expect(hidden.length).toBeGreaterThan(0);
  // The check would see the docs if they were there: the public docs' text did arrive.
  for (const d of shown.slice(0, 3)) expect(received, d.file).toContain(lineOf(d.file));
  for (const d of hidden) {
    const line = lineOf(d.file);
    expect(line, `${d.file} has a plain line to look for`).toBeTruthy();
    expect(received.includes(line), `${d.file} reached a reader`).toBe(false);
  }
});
