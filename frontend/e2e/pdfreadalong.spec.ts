// Read-along on a text PDF's pages (docs/pdf-read-along.md): the narration highlights the
// sentence on the pdf.js page, clicking a sentence plays from it, the pages follow the audio,
// and all of it works offline from a download.
import { expect, test, type Page } from "@playwright/test";
import { controlled, downloadBook, shot, signIn, userOf, workId } from "./helpers";

const KEEPER = "The Keeper of Small Lights";

test.describe.configure({ mode: "serial" });

/** Move the narration slider to `s` seconds (a range input: set its value as a person would). */
async function scrub(page: Page, s: number) {
  await page.getByLabel("Narration position").evaluate((el, v) => {
    const input = el as HTMLInputElement;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, String(v));
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }, s);
}

const pageNumber = (page: Page) => page.getByLabel("Go to page").inputValue();

test("read along on the PDF's pages, then offline", async ({ page, context }, info) => {
  await signIn(page, userOf(info));
  await controlled(page);
  const id = await workId(page, KEEPER);
  const fileId = await page.evaluate(async (w) => {
    const works = (await (await fetch("/api/library")).json()).works as { id: number; files: { id: number; media_type: string }[] }[];
    return works.find((x) => x.id === w)!.files.find((f) => f.media_type === "application/pdf")!.id;
  }, id);
  // A sentence over several lines of the left column, as the server maps it (PDF points, A5 page).
  const target = await page.evaluate(async (f) => {
    const r = (await (await fetch(`/api/files/${f}/sentences`)).json()) as { sentences: { work_n: number | null; page: number; boxes: number[][] }[] };
    return r.sentences.find((s) => s.work_n != null && s.page === 0 && s.boxes.length > 2)!.boxes[0];
  }, fileId);

  // The book card says what kind of PDF this is, and that the narrated EPUB is EPUB-only.
  await page.goto(`/app/library/${id}`);
  await expect(page.locator('[data-state="pdf-text"]')).toContainText("Text PDF: read-along highlights the narration on its pages");
  await expect(page.locator('[data-state="export-needs-epub"]')).toContainText("Narrated EPUB export needs an EPUB edition");
  await page.getByRole("link", { name: `Read along on the PDF pages of ${KEEPER}` }).click();

  const player = page.getByRole("region", { name: "Read-along player" });
  await expect(player).toBeVisible({ timeout: 20_000 });
  await expect(page.locator(".pages-page")).toBeVisible();

  // Clicking a sentence on the page plays from it and highlights it.
  const canvas = page.locator(".pdf-page-wrap").first();
  const box = (await canvas.boundingBox())!;
  const scale = box.width / 420;
  const [, x0, y0, x1, y1] = target;
  await canvas.click({ position: { x: ((x0 + x1) / 2) * scale, y: (595 - (y0 + y1) / 2) * scale } });
  await expect(page.locator(".pdf-ra-mark").first()).toBeVisible();
  expect(await page.locator(".pdf-ra-mark").count()).toBeGreaterThan(2); // every line of the sentence
  await shot(page, info, "pdf-readalong");

  // Following: narration on the last page turns to it.
  await scrub(page, 140);
  await expect.poll(() => pageNumber(page)).toBe("3");
  await expect(page.locator(".pdf-ra-mark").first()).toBeVisible();
  // Not following: the page stays where the reader is.
  await page.getByRole("button", { name: "Follow the narration" }).click();
  await page.getByLabel("Go to page").fill("1");
  await scrub(page, 145);
  await page.waitForTimeout(500);
  expect(await pageNumber(page)).toBe("1");
  await page.getByRole("button", { name: "Follow the narration" }).click(); // follow again: back to page 3
  await expect.poll(() => pageNumber(page)).toBe("3");
  await page.evaluate(() => document.querySelector("audio")?.pause());

  // Download with read-along, then read along with the network off.
  await downloadBook(page, id);
  await page.goto("/app/downloads");
  await expect(page.getByTestId(`downloaded-${id}`)).toContainText("read-along");
  await context.setOffline(true);
  await page.reload();
  await page.goto(`/app/reader/${id}?file=${fileId}`);
  await expect(page.locator(".net-status")).toHaveText("Offline");
  await expect(player).toBeVisible({ timeout: 20_000 });
  await scrub(page, 60);
  await expect.poll(() => pageNumber(page)).not.toBe("3"); // the narration is earlier in the book
  await expect(page.locator(".pdf-ra-mark").first()).toBeVisible();
  await shot(page, info, "pdf-readalong-offline");
  await context.setOffline(false);
});
