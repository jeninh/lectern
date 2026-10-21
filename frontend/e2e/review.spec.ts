// Multi-select in Review before filing: uploads waiting to be filed.
import { expect, test } from "@playwright/test";
import { countText, library, signIn } from "./helpers";
import path from "node:path";
import { fileURLToPath } from "node:url";

const UPLOADS = [1, 2, 3].map((i) => path.join(path.dirname(fileURLToPath(import.meta.url)), ".out", `upload-${i}.epub`)); // e2e/seed.py

test("select uploads, Esc clears without closing, file some and discard the rest", async ({ page }) => {
  page.on("dialog", (d) => d.accept());
  await signIn(page);
  await page.locator('input[type="file"][accept]').setInputFiles(UPLOADS);
  await expect(page.getByText("3 books uploaded and waiting")).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "Review", exact: true }).first().click();
  const sheet = page.getByRole("dialog", { name: "Review before filing" });
  const card = (t: string) => sheet.getByRole("gridcell", { name: t });
  await expect(sheet.getByRole("grid", { name: "Uploads waiting" })).toBeVisible();
  await expect(sheet.getByRole("gridcell")).toHaveCount(3);

  // Click on the card itself selects; its fields and buttons keep working.
  await card("Upload One").locator(".import-title").click();
  await expect(countText(page)).toHaveText("1 selected");
  await card("Upload One").getByRole("button", { name: /Edit details/ }).click();
  await expect(card("Upload One").getByRole("textbox").first()).toBeVisible();
  await card("Upload One").getByRole("textbox").first().press("Control+a"); // the field's text
  await expect(countText(page)).toHaveText("1 selected");

  await card("Upload One").locator(".import-title").click();
  await page.keyboard.press("Control+a");
  await expect(countText(page)).toHaveText("All 3 selected");
  await page.keyboard.press("Escape"); // clears, the sheet stays
  await expect(sheet).toBeVisible();
  await expect(sheet.getByTestId("selection-bar")).toHaveCount(0);

  const cards = sheet.getByRole("gridcell");
  const names = await cards.evaluateAll((els) => els.map((e) => e.getAttribute("aria-label")));
  await cards.nth(0).locator(".import-title").click();
  await cards.nth(1).locator(".pick").click(); // the check circle adds, like Ctrl+click
  await expect(countText(page)).toHaveText("2 selected");
  await sheet.getByRole("button", { name: "File 2" }).click();
  await expect(page.locator(".toast").filter({ hasText: "Filed 2 books" })).toBeVisible();
  await expect(sheet.getByRole("gridcell")).toHaveCount(1);
  const titles = (await library(page)).map((w) => w.title);
  expect(titles).toEqual(expect.arrayContaining([names[0], names[1]]));

  await sheet.getByRole("gridcell").first().locator(".import-title").click();
  await sheet.getByRole("button", { name: "Discard", exact: true }).last().click();
  await expect(page.locator(".toast").filter({ hasText: "Discarded 1 book" })).toBeVisible();
  await expect(sheet.getByText("Nothing waiting")).toBeVisible();
});
