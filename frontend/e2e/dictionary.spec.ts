// Word lookup in the EPUB reader (issue #78, docs/dictionary.md): with no dictionary the popover
// says how to add one; with a StarDict dictionary installed, double-clicking a word shows its
// definition, and nothing is asked of any other host.
import { expect, type Frame, type Page, test } from "@playwright/test";
import { idOf, signIn } from "./desktop-helpers";

/** A StarDict dictionary as an uncompressed tar (the server also takes .zip and .tar.gz). */
function stardictTar(entries: [string, string][]): Buffer {
  entries = [...entries].sort((a, b) => a[0].localeCompare(b[0]));
  const data: Buffer[] = [];
  const idx: Buffer[] = [];
  let at = 0;
  for (const [word, body] of entries) {
    const b = Buffer.from(body);
    const pos = Buffer.alloc(8);
    pos.writeUInt32BE(at, 0);
    pos.writeUInt32BE(b.length, 4);
    idx.push(Buffer.from(word + "\0"), pos);
    data.push(b);
    at += b.length;
  }
  const idxBuf = Buffer.concat(idx);
  const ifo = `StarDict's dict ifo file\nversion=2.4.2\nbookname=E2E English\nwordcount=${entries.length}\nidxfilesize=${idxBuf.length}\nsametypesequence=m\n`;
  const files: [string, Buffer][] = [["d/e2e.ifo", Buffer.from(ifo)], ["d/e2e.idx", idxBuf], ["d/e2e.dict", Buffer.concat(data)]];
  const out: Buffer[] = [];
  for (const [name, body] of files) {
    const h = Buffer.alloc(512);
    h.write(name, 0);
    h.write("0000644\0", 100);
    h.write("0000000\0", 108);
    h.write("0000000\0", 116);
    h.write(body.length.toString(8).padStart(11, "0") + "\0", 124);
    h.write("00000000000\0", 136);
    h.write("        ", 148);
    h.write("0", 156);
    h.write("ustar\0" + "00", 257);
    let sum = 0;
    for (const byte of h) sum += byte;
    h.write(sum.toString(8).padStart(6, "0") + "\0 ", 148);
    out.push(h, body, Buffer.alloc((512 - (body.length % 512)) % 512));
  }
  out.push(Buffer.alloc(1024));
  return Buffer.concat(out);
}

async function bookFrame(page: Page): Promise<Frame> {
  let found: Frame | undefined;
  await expect.poll(async () => {
    for (const f of page.frames()) {
      if (f === page.mainFrame()) continue;
      if (await f.locator("p", { hasText: "Hello." }).count().catch(() => 0)) found = f;
    }
    return !!found;
  }).toBe(true);
  return found!;
}

async function lookUpHello(page: Page) {
  const frame = await bookFrame(page);
  await frame.locator("p", { hasText: "Hello." }).first().dblclick({ position: { x: 4, y: 6 } });
  const pop = page.getByRole("dialog", { name: /^Dictionary: Hello/ });
  await expect(pop).toBeVisible();
  return pop;
}

test("with no dictionary, the popover says how to add one", async ({ page }) => {
  await signIn(page, "/app");
  const id = await idOf(page, "Book 001");
  await page.goto(`/app/reader/${id}`);
  const pop = await lookUpHello(page);
  await expect(pop).toContainText("No dictionary is available on this server yet");
  await expect(pop.getByRole("link", { name: "Admin → Dictionaries" })).toBeVisible();
  await page.screenshot({ path: "test-results/dictionary-empty.png" });
  await page.keyboard.press("Escape");
  await expect(pop).toBeHidden();
});

test("double-clicking a word shows its definition from the server's dictionary", async ({ page }) => {
  await signIn(page, "/app");
  const me = await (await page.request.get("/api/me")).json();
  const origin = new URL(page.url()).origin;
  const r = await page.request.post("/api/admin/dictionaries?lang=en", {
    data: stardictTar([
      ["hello", "interj. Used as a greeting.\nUsed to attract attention."],
      ["world", "n. The earth."],
    ]),
    headers: { "x-csrf-token": me.csrf_token, origin, "content-type": "application/x-tar" },
  });
  expect(r.status(), await r.text()).toBe(201);

  const id = await idOf(page, "Book 001");
  const elsewhere: string[] = [];
  page.on("request", (req) => {
    if (new URL(req.url()).origin !== origin && !req.url().startsWith("blob:") && !req.url().startsWith("data:")) elsewhere.push(req.url());
  });
  await page.goto(`/app/reader/${id}`);
  const lookups = page.waitForRequest((req) => req.url().includes("/api/dictionary/lookup"));
  const pop = await lookUpHello(page);
  expect(new URL((await lookups).url()).searchParams.get("lang")).toBe("en");
  await expect(pop.locator(".dict-word")).toContainText("hello");
  await expect(pop.locator(".dict-pos")).toHaveText(/interjection/);
  await expect(pop.getByRole("listitem")).toHaveText(["Used as a greeting.", "Used to attract attention."]);
  await expect(pop).toContainText("E2E English");
  await page.screenshot({ path: "test-results/dictionary-popover.png" });
  // A click in the book, outside the popover, closes it.
  await (await bookFrame(page)).locator("p", { hasText: "Hello." }).first().click({ position: { x: 40, y: 6 } });
  await expect(pop).toBeHidden();
  expect(elsewhere).toEqual([]);

  await page.goto("/app/admin#dictionaries");
  const card = page.locator("#dictionaries");
  await expect(card).toContainText("E2E English");
  await expect(card).toContainText("2 entries");
  await card.scrollIntoViewIfNeeded();
  await card.screenshot({ path: "test-results/dictionary-admin.png" });
});
