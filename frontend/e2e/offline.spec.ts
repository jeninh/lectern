// Download a book, then read, listen, and read along with the network off.
import { expect, test } from "@playwright/test";
import { apiAs, cacheKeys, controlled, downloadBook, MAGI, shot, signIn, userOf, workId } from "./helpers";

test.describe.configure({ mode: "serial" });

test("download a book, then read, listen, and read along offline", async ({ page, context, baseURL }, info) => {
  const user = userOf(info);
  await signIn(page, user);
  await controlled(page);
  const id = await workId(page, MAGI);

  // Download: EPUB, data-saver audio, read-along (the defaults for this book).
  await page.goto(`/app/library/${id}`);
  await page.getByTestId("download-offline").click();
  const sheet = page.getByRole("dialog");
  await expect(sheet.getByText("Book (EPUB)")).toBeVisible();
  await expect(sheet.getByRole("switch", { name: /Full quality/ })).toHaveAttribute("aria-checked", "false");
  await expect(sheet.getByText(/free for Lectern on this device/)).toBeVisible();
  await shot(page, info, "download-sheet");
  await sheet.getByTestId("download-start").click();
  await expect(page.locator(".toast").filter({ hasText: "is on this device" })).toBeVisible({ timeout: 30_000 });

  await page.goto("/app/downloads");
  const item = page.getByTestId(`downloaded-${id}`);
  await expect(item).toContainText("EPUB · audiobook (data saver) · read-along");
  await expect(page.getByTestId("persisted")).toBeVisible();
  await expect(page.getByText(/available to Lectern used/)).toBeVisible();

  // Only the shell and the downloaded files are in Cache Storage: no API response, ever.
  const keys = await cacheKeys(page);
  const books = keys["lectern-books"] ?? [];
  expect(books).toEqual(expect.arrayContaining([`/library/works/${id}/book.epub`, expect.stringMatching(/^\/library\/tracks\/\d+\/stream\?variant=low$/), expect.stringMatching(new RegExp(`^/library/works/${id}/cover\\.jpg`))]));
  for (const urls of Object.values(keys)) for (const u of urls) expect(u).not.toMatch(/^\/(api|login|logout|setup|opds|mcp)\b/);

  // Offline: the app opens from the service worker and says it is offline.
  await context.setOffline(true);
  await page.reload();
  await expect(page.locator(".net-status")).toHaveText("Offline");
  await expect(page.locator(".offline-banner")).toContainText("1 downloaded book opens");
  await expect(item).toBeVisible();
  await shot(page, info, "downloads-offline");

  // Reader.
  await item.getByRole("link", { name: "Read", exact: true }).click();
  await expect(page.locator(".reader-msg")).toHaveCount(0, { timeout: 20_000 });
  await expect(page.locator("foliate-view")).toBeAttached();
  await page.waitForTimeout(800);
  await page.getByRole("button", { name: "Next page" }).click();
  // Offline the pill says so; the queued position is in its label.
  await expect(page.locator(".net-status")).toHaveAttribute("aria-label", /1 reading position waits to sync/, { timeout: 10_000 });
  // Foliate renders into iframes inside (closed) shadow roots; wait for text in one of them.
  await expect
    .poll(async () => {
      for (const f of page.frames().slice(1)) {
        const t = await f.evaluate(() => document.body?.innerText ?? "").catch(() => "");
        if (t.trim().length > 20) return true;
      }
      return false;
    }, { timeout: 15_000 })
    .toBe(true);
  // Headless WebKit sometimes leaves foliate's page unpainted after an in-app navigation (the
  // text is in the DOM). A fresh load of the reader, still offline, paints reliably.
  await page.reload();
  await expect(page.locator(".reader-msg")).toHaveCount(0, { timeout: 20_000 });
  await page.waitForTimeout(1500);
  await expect(page.locator(".net-status")).toHaveText("Offline");
  await expect(page.locator(".net-status")).toHaveAttribute("aria-label", /1 reading position waits to sync/);
  await shot(page, info, "reader-offline");

  // Listening: the audio comes from the copy on this device, ranges and all.
  const stream = (await cacheKeys(page))["lectern-books"].find((u) => u.includes("/stream"))!;
  const res = await page.evaluate(async (u) => {
    const r = await fetch(u, { headers: { range: "bytes=100-199" } });
    return { status: r.status, range: r.headers.get("content-range"), offline: r.headers.get("x-lectern-offline"), size: (await r.arrayBuffer()).byteLength };
  }, stream);
  expect(res).toMatchObject({ status: 206, offline: "1", size: 100 });
  expect(res.range).toMatch(/^bytes 100-199\/\d+$/);

  await page.goto("/app/downloads");
  await page.getByTestId(`downloaded-${id}`).getByRole("button", { name: "Listen" }).click();
  const audioSrc = await page.waitForFunction(() => document.querySelector("audio")?.getAttribute("src") || null);
  expect(await audioSrc.jsonValue()).toMatch(/\/library\/tracks\/\d+\/stream\?variant=low$/);
  await expect(page.locator(".mini-player")).toContainText(MAGI);
  // Lock-screen and headset controls, offline too.
  const session = () => page.evaluate(() => ("mediaSession" in navigator ? { title: navigator.mediaSession.metadata?.title ?? "", art: navigator.mediaSession.metadata?.artwork?.[0]?.src ?? "" } : null));
  if (await session()) {
    // The metadata is set once playback starts, so it can lag the play button by a moment.
    await expect.poll(async () => (await session())!.title).toContain(MAGI);
    expect((await session())!.art).toMatch(new RegExp(`/library/works/${id}/cover\.jpg`));
  }

  // Read-along: text, sentences, and highlighting from IndexedDB.
  await page.goto(`/app/read/${id}`);
  const sentence = page.locator(".ra-s[data-m]").nth(3);
  await expect(sentence).toBeVisible({ timeout: 20_000 });
  await page.getByRole("button", { name: "Next sentence" }).click();
  await expect(page.locator(".ra-s.is-current").first()).toBeVisible();
  await sentence.click();
  await expect(sentence).toHaveClass(/is-current/);
  await shot(page, info, "readalong-offline");

  // Back online: the queued positions sync, and the server has this device's position.
  await context.setOffline(false);
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await expect(page.locator(".net-status")).toHaveCount(0, { timeout: 20_000 });
  const other = await apiAs(baseURL!, user);
  const prog = (await (await other.api.get(`/api/works/${id}/progress`)).json()) as { source: string; device: string };
  expect(prog.source).toMatch(/web|audio/);
  await other.api.dispose();
});
