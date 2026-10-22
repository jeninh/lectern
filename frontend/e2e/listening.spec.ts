// Listening: the page scrolls freely while an audiobook plays (issue #36).
import { expect, test, type Page } from "@playwright/test";
import { MAGI, signIn, userOf } from "./helpers";

// Desktop-sized window with a mouse wheel, in each phone project's engine (WebKit and Chromium).
// No service worker, so the routes below see the app's API requests.
test.use({ viewport: { width: 1280, height: 800 }, isMobile: false, hasTouch: false, serviceWorkers: "block" });

/** A long audiobook (40 chapters) and enough other audiobooks that the page itself scrolls. */
async function longListening(page: Page) {
  await page.route("**/api/works/*/tracks", async (r) => {
    const res = await r.fetch();
    const j = await res.json();
    for (const t of j.tracks) t.chapters = Array.from({ length: 40 }, (_, i) => ({ title: `Chapter ${i + 1}`, start_s: (i * t.duration_s) / 40 }));
    await r.fulfill({ response: res, json: j });
  });
  await page.route("**/api/listening", async (r) => {
    const res = await r.fetch();
    const j = await res.json();
    const last = j.items[j.items.length - 1];
    const more = Array.from({ length: 24 }, (_, i) => ({ ...last, id: 900000 + i, title: `Another audiobook ${i + 1}` }));
    await r.fulfill({ response: res, json: { ...j, items: [...j.items, ...more] } });
  });
}

const scrollY = (page: Page) => page.evaluate(() => Math.round(window.scrollY));

test("the page scrolls with the wheel over the chapter list while audio plays, and stays put", async ({ page }, info) => {
  await longListening(page);
  await signIn(page, userOf(info));
  await page.goto("/app/listening");
  await page.getByRole("heading", { name: "Listening", level: 1 }).waitFor();
  // The most recent audiobook may already be queued (paused); otherwise start it from its card.
  const card = page.getByRole("button", { name: new RegExp(`^Play ${MAGI}`) });
  const play = page.locator(".now-playing").getByRole("button", { name: "Play", exact: true });
  await expect(card.or(play).first()).toBeVisible();
  await (await play.count() ? play : card).click();
  const audio = page.locator("audio");
  await expect.poll(() => audio.evaluate((a: HTMLAudioElement) => !a.paused && a.currentTime > 0)).toBe(true);

  // A long chapter list is part of the page, not a scroll box that swallows the wheel.
  const chapters = page.locator("ol.chapters");
  await expect(chapters.locator('[aria-current="true"]')).toBeVisible();
  const box = (await chapters.boundingBox())!;
  await page.mouse.move(box.x + 60, box.y + 40);
  const before = await scrollY(page);
  for (let i = 0; i < 5; i++) {
    await page.mouse.wheel(0, 100);
    await page.waitForTimeout(50);
  }
  await expect.poll(() => scrollY(page)).toBe(before + 500);

  // The position holds through several progress ticks.
  const held = await scrollY(page);
  const t0 = await audio.evaluate((a: HTMLAudioElement) => a.currentTime);
  await page.waitForTimeout(2000);
  expect(await audio.evaluate((a: HTMLAudioElement) => a.currentTime)).toBeGreaterThan(t0 + 1);
  expect(await scrollY(page)).toBe(held);

  // A window around the current chapter; every chapter is still reachable.
  await expect(chapters.getByRole("button")).toHaveCount(8);
  await page.getByRole("button", { name: "Show all 40 chapters" }).click();
  await expect(chapters.getByRole("button")).toHaveCount(40);
  await page.getByRole("button", { name: "Show fewer chapters" }).click();
  await expect(chapters.getByRole("button")).toHaveCount(8);
});
