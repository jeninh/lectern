import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanQuery, clearLookupCache, lookupWord, placePopover, wordAt } from "../dictionary";

describe("cleanQuery", () => {
  it.each([
    ["  “Running,” ", "Running"],
    ["ice\n  cream.", "ice cream"],
    ["l’homme", "l’homme"],
    ["Café!", "Café"],
  ])("%j → %j", (raw, want) => expect(cleanQuery(raw)).toBe(want));

  it("refuses what is not a word or short phrase", () => {
    expect(cleanQuery("")).toBeNull();
    expect(cleanQuery(" 1984 ")).toBeNull();
    expect(cleanQuery("— …")).toBeNull();
    expect(cleanQuery("one two three four five six seven")).toBeNull();
    expect(cleanQuery("x".repeat(101))).toBeNull();
  });
});

describe("wordAt", () => {
  const line = "She said: “don’t run-away,” then left.";
  it("finds the word under a character, keeping apostrophes and hyphens", () => {
    expect(wordAt(line, line.indexOf("run") + 1)).toBe("run-away");
    expect(wordAt(line, line.indexOf("don"))).toBe("don’t");
    expect(wordAt(line, 0)).toBe("She");
    expect(wordAt(line, line.length - 2)).toBe("left");
  });
  it("takes the word just before a gap, and nothing between words", () => {
    expect(wordAt("cat dog", 3)).toBe("cat");
    expect(wordAt("a  b", 2)).toBeNull();
  });
  it("counts UTF-16 units the way the PDF text does", () => {
    const s = "𝔸 big word";
    expect(wordAt(s, s.indexOf("word"))).toBe("word");
  });
});

describe("placePopover", () => {
  const size = { w: 300, h: 200 };
  const view = { w: 800, h: 600 };
  it("sits below the word, centred on it", () => {
    expect(placePopover({ left: 390, top: 100, bottom: 120, width: 20 }, size, view)).toEqual({ left: 250, top: 128, below: true });
  });
  it("goes above when there is no room below", () => {
    expect(placePopover({ left: 390, top: 500, bottom: 520, width: 20 }, size, view)).toEqual({ left: 250, top: 292, below: false });
  });
  it("stays inside the window at the edges", () => {
    expect(placePopover({ left: 790, top: 100, bottom: 120, width: 10 }, size, view).left).toBe(492);
    expect(placePopover({ left: 0, top: 100, bottom: 120, width: 10 }, size, view).left).toBe(8);
  });
});

describe("lookupWord", () => {
  afterEach(() => {
    clearLookupCache();
    vi.unstubAllGlobals();
  });
  const answer = { query: "run", lang: "en", to: "fr", installed: true, dictionaries: 1, languages: ["en"], targets: [], entries: [], translations: [] };

  it("asks the server once per word, language, and target in a session", async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify(answer), { status: 200, headers: { "content-type": "application/json" } }));
    vi.stubGlobal("fetch", fetch);
    await lookupWord("run", "en", "fr");
    await lookupWord("run", "en", "fr");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0]).toEqual([expect.stringMatching(/^\/api\/dictionary\/lookup\?q=run&lang=en&to=fr$/), expect.anything()]);
    await lookupWord("run", "en", "de");
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("does not keep a failure", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ detail: "too_many_lookups" }), { status: 429, headers: { "content-type": "application/json" } }));
    fetch.mockResolvedValueOnce(new Response(JSON.stringify(answer), { status: 200, headers: { "content-type": "application/json" } }));
    vi.stubGlobal("fetch", fetch);
    await expect(lookupWord("run", "en", null)).rejects.toThrow();
    await expect(lookupWord("run", "en", null)).resolves.toMatchObject({ query: "run" });
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
