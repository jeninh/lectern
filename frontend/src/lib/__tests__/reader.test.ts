import { describe, expect, it } from "vitest";
import { bookCss, clean, DEFAULTS, effective, fontFaces, reconcile } from "../reader";

const ORIGIN = "https://lectern.tailnet.ts.net";

describe("bookCss", () => {
  it("leaves the book's own font and spacing alone by default", () => {
    const css = bookCss(DEFAULTS, "light", ORIGIN);
    expect(css).not.toMatch(/font-family|@font-face|letter-spacing|word-spacing|margin-block|text-stroke/);
    expect(css).toContain("line-height: 1.5 !important");
  });

  it("loads a dyslexia-friendly font from this server only, absolute so the book's pages resolve it", () => {
    const css = bookCss({ ...DEFAULTS, font: "dyslexic" }, "sepia", ORIGIN);
    expect(css).toContain(`font-family: "Lectern OpenDyslexic"`);
    const urls = [...css.matchAll(/url\("([^"]+)"\)/g)].map((m) => m[1]);
    expect(urls.length).toBe(4); // regular, italic, bold, bold italic
    for (const u of urls) {
      expect(new URL(u).origin).toBe(ORIGIN);
      expect(u).toMatch(/opendyslexic.*\.woff2/);
    }
    // Only the chosen font's files, not every bundled font.
    expect(css).not.toMatch(/Lexend|Atkinson/);
  });

  it("every bundled font is a woff2 on this server", () => {
    const urls = [...fontFaces(ORIGIN).matchAll(/url\("([^"]+)"\)/g)].map((m) => m[1]);
    expect(urls.length).toBe(14);
    expect(urls.every((u) => u.startsWith(`${ORIGIN}/`) && u.includes(".woff2"))).toBe(true);
  });

  it("applies spacing, weight, and the high-contrast theme", () => {
    const css = bookCss({ ...DEFAULTS, letterSpacing: 0.05, wordSpacing: 0.2, paragraphSpacing: 1.5, heavier: true }, "contrast", ORIGIN);
    expect(css).toContain("letter-spacing: 0.05em !important;");
    expect(css).toContain("word-spacing: 0.2em !important;");
    expect(css).toContain("p { margin-block: 0 1.5em !important; }");
    expect(css).toContain("-webkit-text-stroke");
    expect(css).toContain("background: #ffffff !important; color: #000000 !important;");
    expect(css).toContain("text-decoration: underline");
  });
});

describe("override merge", () => {
  it("takes the book's fields over the default's over the app's", () => {
    const s = effective({ theme: "sepia", size: 120, font: "lexend" }, { size: 85 });
    expect(s).toEqual({ ...DEFAULTS, theme: "sepia", size: 85, font: "lexend" });
  });

  it("an empty override is the default; no override is too", () => {
    const def = { theme: "dark" as const, lineHeight: 1.8 };
    expect(effective(def, {})).toEqual(effective(def, null));
    expect(effective(null, null)).toEqual(DEFAULTS);
  });

  it("drops what the server would refuse: unknown fields, unknown choices, wrong types; clamps ranges", () => {
    expect(clean({ theme: "neon", font: "Comic", size: 1000, letterSpacing: -1, justify: "yes", colour: "red", flow: "scrolled" })).toEqual({ size: 300, letterSpacing: 0, flow: "scrolled" });
    expect(clean("x")).toBeNull();
    expect(clean([1])).toBeNull();
  });
});

describe("reconcile (migration from lectern.reader and offline changes)", () => {
  const legacy = { flow: "scrolled" as const, size: 130, lineHeight: 1.7, font: "serif" as const, width: "wide" as const, margin: "small" as const, theme: "sepia" as const, justify: false };

  it("first load after the update: the browser's old lectern.reader becomes the synced default", () => {
    const { use, push } = reconcile({ default: null, book: null }, { default: legacy, book: null }, { default: false, book: false });
    expect(use.default).toEqual(legacy);
    expect(push).toEqual({ default: legacy });
  });

  it("another browser later: its own old value does not overwrite the synced default", () => {
    const { use, push } = reconcile({ default: { theme: "dark" }, book: null }, { default: legacy, book: null }, { default: false, book: false });
    expect(use.default).toEqual({ theme: "dark" });
    expect(push).toEqual({});
  });

  it("nothing anywhere: nothing to push", () => {
    expect(reconcile({ default: null, book: null }, { default: null, book: null }, { default: false, book: false })).toEqual({ use: { default: null, book: null }, push: {} });
  });

  it("a change made offline wins and is sent, including turning 'Just this book' off", () => {
    const server = { default: { size: 100 }, book: { size: 80 } };
    const r = reconcile(server, { default: { size: 140 }, book: null }, { default: true, book: true });
    expect(r.use).toEqual({ default: { size: 140 }, book: null });
    expect(r.push).toEqual({ default: { size: 140 }, book: null });
  });

  it("without pending changes the server's book override wins over a stale copy", () => {
    const r = reconcile({ default: null, book: { size: 80 } }, { default: null, book: { size: 120 } }, { default: false, book: false });
    expect(r.use.book).toEqual({ size: 80 });
    expect(r.push).toEqual({});
  });
});
