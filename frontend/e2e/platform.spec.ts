// Platform-correct modifiers and touch selection mode.
import { expect, test, type Locator } from "@playwright/test";
import { book, countText, selectedTitles, signIn, titles } from "./helpers";

test.describe("on a Mac", () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => Object.defineProperty(Navigator.prototype, "platform", { get: () => "MacIntel" }));
    await signIn(page);
    await expect(book(page, "Book 001")).toBeVisible();
  });

  test("Cmd toggles and Cmd+A selects all; Ctrl+click and Ctrl+A are not ours", async ({ page }) => {
    await book(page, "Book 001").click();
    await book(page, "Book 003").click({ modifiers: ["Meta"] });
    expect(await selectedTitles(page)).toEqual(["Book 001", "Book 003"]);
    // Ctrl+click is a secondary click on a Mac: the selection stays as it was.
    await book(page, "Book 005").click({ modifiers: ["Control"] });
    expect(await selectedTitles(page)).toEqual(["Book 001", "Book 003"]);
    await book(page, "Book 006").click({ modifiers: ["Meta", "Shift"] });
    expect(await selectedTitles(page)).toEqual(["Book 001", ...titles(3, 6)]);
    await page.keyboard.press("Control+a");
    await expect(countText(page)).toHaveText("5 selected");
    await page.keyboard.press("Meta+a");
    await expect(countText(page)).toHaveText("All 360 selected");
    await page.keyboard.press("Meta+ArrowDown"); // Cmd+Arrow jumps to the end
    await expect(book(page, "Book 360")).toBeFocused();
  });
});

test.describe("on a touch screen", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

  /** A finger press: pointer events with pointerType "touch", then the click the browser sends. */
  async function touch(cell: Locator, holdMs = 0) {
    const box = (await cell.boundingBox())!;
    const at = { pointerType: "touch", isPrimary: true, clientX: box.x + 20, clientY: box.y + 20, bubbles: true };
    await cell.dispatchEvent("pointerdown", at);
    if (holdMs) await cell.page().waitForTimeout(holdMs);
    await cell.dispatchEvent("pointerup", at);
    await cell.dispatchEvent("click", { detail: 1, clientX: at.clientX, clientY: at.clientY });
  }

  test("long press starts selection mode, taps toggle, Done leaves, a tap opens", async ({ page }) => {
    await signIn(page);
    await expect(book(page, "Book 001")).toBeVisible();
    // A press that moves (a scroll) is not a long press.
    const cell = book(page, "Book 002");
    const box = (await cell.boundingBox())!;
    const at = { pointerType: "touch", isPrimary: true, clientX: box.x + 20, clientY: box.y + 20, bubbles: true };
    await cell.dispatchEvent("pointerdown", at);
    await cell.dispatchEvent("pointermove", { ...at, clientY: at.clientY + 40 });
    await page.waitForTimeout(650);
    await cell.dispatchEvent("pointercancel", at);
    await expect(page.getByTestId("selection-bar")).toHaveCount(0);

    await touch(book(page, "Book 002"), 650);
    await expect(countText(page)).toHaveText("1 selected");
    await expect(book(page, "Book 002")).toHaveAttribute("data-selecting", "true");
    await touch(book(page, "Book 003"));
    await touch(book(page, "Book 004"));
    await touch(book(page, "Book 002"));
    expect(await selectedTitles(page)).toEqual(["Book 003", "Book 004"]);
    await expect(page.getByTestId("selection-bar")).toBeInViewport();
    await page.screenshot({ path: "test-results/touch-selection.png" });

    await page.getByRole("button", { name: "Done" }).click();
    await expect(page.getByTestId("selection-bar")).toHaveCount(0);
    await touch(book(page, "Book 001"));
    await expect(page.getByRole("dialog", { name: "Book 001" })).toBeVisible();
  });
});
