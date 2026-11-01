// The Jobs page (#87) against the jobs e2e/seed.py queues (background jobs are off, so they
// stay put): a bulk batch as one row, pause, reorder, retry, cancel, and pausing the queue.
import { expect, test, type Page } from "@playwright/test";
import { signIn } from "./desktop-helpers";

const section = (page: Page, name: string) => page.getByRole("region", { name: new RegExp(`^${name}`) });
const firstBooks = (page: Page, name: string) => section(page, name).locator("tbody tr td:first-child strong").allTextContents();

test("owner sees, pauses, reorders, retries, and cancels jobs", async ({ page }) => {
  page.on("dialog", (d) => d.accept());
  await signIn(page, "/app/jobs");
  await expect(page.getByRole("heading", { name: "Jobs", level: 1 })).toBeVisible();

  // The bulk conversion is one row; expanding it shows each book.
  const waiting = section(page, "Waiting");
  const batch = waiting.getByRole("button", { name: "OCR, 3 books", exact: true });
  await expect(waiting.getByText("0 of 3 done · 3 waiting")).toBeVisible();
  await expect(page.getByText("Book 305")).toHaveCount(0);
  await batch.click();
  await expect(batch).toHaveAttribute("aria-expanded", "true");
  await expect(waiting.getByText("Book 305")).toBeVisible();

  // Pausing the rest of the batch moves it under Paused.
  await page.getByRole("button", { name: "Pause rest: OCR, 3 books" }).click();
  await expect(section(page, "Paused").getByText("0 of 3 done · 3 paused")).toBeVisible();

  // OCR jumps the line.
  expect((await firstBooks(page, "Waiting"))[0]).toBe("Book 301");
  await page.getByRole("button", { name: "Move to top: OCR, Book 302" }).click();
  await expect.poll(async () => (await firstBooks(page, "Waiting"))[0]).toBe("Book 302");

  // Retry the failed conversion; cancel the narration.
  await expect(section(page, "Failed").getByText("Failed: Tesseract crashed")).toBeVisible();
  await page.getByRole("button", { name: "Retry: OCR, Book 303" }).click();
  await expect(section(page, "Failed")).toHaveCount(0);
  await expect(section(page, "Waiting").getByText("Book 303")).toBeVisible();
  await page.getByRole("button", { name: "Cancel: Audiobook (Kokoro), Book 307" }).click();
  await expect(section(page, "Done").getByText("Cancelled")).toBeVisible();

  // The whole queue.
  await page.getByRole("button", { name: "Pause queue" }).click();
  await expect(page.getByText("The queue is paused: no new job starts.")).toBeVisible();
  await expect(section(page, "Waiting").getByText("Queue paused").first()).toBeVisible();
  await page.getByRole("button", { name: "Resume queue" }).click();
  await expect(page.getByText("The queue is paused: no new job starts.")).toHaveCount(0);
});

test("a reader sees only their own jobs", async ({ page }) => {
  await signIn(page, "/app/jobs", "reader");
  await expect(page.getByText("No jobs")).toBeVisible();
  await expect(page.getByRole("button", { name: "Pause queue" })).toHaveCount(0);
});
