// @vitest-environment jsdom
// The vocabulary list's client side (issue #80): the sentence kept with a word, the save body,
// the link back to the word, and the popover's Save button (nothing is sent until it is pressed).
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { DictionaryPopover, type LookupRequest } from "../../components/DictionaryPopover";
import { clearLookupCache, type DictionaryEntry, type LookupResult } from "../dictionary";
import { saveBody, sentenceAround, vocabularyPath } from "../vocabulary";

vi.mock("../store", () => ({ useStore: () => ({ me: { user: { role: "member" } } }) }));

describe("sentenceAround", () => {
  const text = "First one. Second sentence is here! Third? Last bit";
  const at = (w: string) => [text.indexOf(w), text.indexOf(w) + w.length] as const;

  it("keeps the sentence between terminators, at both edges of the text", () => {
    expect(sentenceAround(text, ...at("First"))).toBe("First one.");
    expect(sentenceAround(text, ...at("sentence"))).toBe("Second sentence is here!");
    expect(sentenceAround(text, ...at("Third"))).toBe("Third?");
  });
  it("runs to the end of the text when the last sentence has no terminator", () => {
    expect(sentenceAround(text, ...at("bit"))).toBe("Last bit");
  });
  it("splits at an abbreviation like Mr. (accepted limitation)", () => {
    const t = "He met Mr. Smith today.";
    expect(sentenceAround(t, t.indexOf("Smith"), t.indexOf("Smith") + 5)).toBe("Smith today.");
  });
  it("does not split inside a word's own punctuation without a following space", () => {
    const t = "Version 3.5 is out. Next.";
    expect(sentenceAround(t, 0, 7)).toBe("Version 3.5 is out.");
  });
  it("caps a long sentence at about 400 characters around the word", () => {
    const long = `${"a ".repeat(500)}target${" b".repeat(500)}`;
    const start = long.indexOf("target");
    const out = sentenceAround(long, start, start + 6);
    expect(out.length).toBeLessThanOrEqual(400);
    expect(out.length).toBeGreaterThan(300);
    expect(out).toContain("target");
  });
});

const entry = (over: Partial<DictionaryEntry> = {}): DictionaryEntry => ({
  headword: "run",
  pos: "verb",
  pronunciation: null,
  definitions: ["to move fast", "to operate"],
  more: 0,
  query: "ran",
  lemma: true,
  dictionary: { id: "d1", name: "Wiktionary", lang: "en", to: "en" },
  ...over,
});

const result = (over: Partial<LookupResult> = {}): LookupResult => ({
  query: "ran",
  lang: "en",
  to: "fr",
  installed: true,
  dictionaries: 1,
  languages: ["en"],
  targets: ["fr"],
  entries: [entry()],
  translations: [],
  ...over,
});

describe("saveBody", () => {
  it("uses the headword as the lemma for a lemma entry, and joins definitions with newlines", () => {
    const b = saveBody(entry(), { query: "ran" }, result());
    expect(b).toMatchObject({ word: "ran", lemma: "run", lang: "en", definition: "to move fast\nto operate", pos: "verb", dictionary: "Wiktionary" });
    expect(b.to).toBeUndefined();
  });
  it("uses the word itself as the lemma for an exact entry", () => {
    const e = entry({ lemma: false, headword: "Ran", query: "ran" });
    expect(saveBody(e, { query: "ran" }, result({ entries: [e] })).lemma).toBe("ran");
  });
  it("adds `to` for a translation, and the place when there is one", () => {
    const t = entry({ lemma: false, headword: "ran", definitions: ["couru"], dictionary: { id: "d2", name: "EN-FR", lang: "en", to: "fr" } });
    const place = { work_id: 4, file_id: 9, spine_index: 2, char: 10, length: 3, context: "He ran." };
    const b = saveBody(t, { query: "ran", place }, result({ entries: [], translations: [t] }));
    expect(b).toMatchObject({ to: "fr", definition: "couru", ...place });
  });
});

describe("vocabularyPath", () => {
  const book = { id: 7, title: "T", author: null };
  it("opens an EPUB word at its passage", () => {
    expect(vocabularyPath({ word: "ran", book, location: { spine_index: 3, char: 120, length: 3 }, page: null, file_id: 11 })).toBe("/app/reader/7?at=3:120&len=3");
  });
  it("opens a PDF word on its page with the word found", () => {
    expect(vocabularyPath({ word: "né & co", book, location: null, page: 12, file_id: 11 })).toBe(`/app/reader/7?file=11&page=12&find=${encodeURIComponent("né & co")}`);
  });
  it("is null without a book or a place", () => {
    expect(vocabularyPath({ word: "x", book: null, location: null, page: 2, file_id: 1 })).toBeNull();
    expect(vocabularyPath({ word: "x", book, location: null, page: null, file_id: null })).toBeNull();
  });
});

describe("DictionaryPopover Save", () => {
  let el: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;
  let fetchMock: ReturnType<typeof vi.fn>;
  const place = { work_id: 4, file_id: 9, spine_index: 2, char: 10, length: 3, context: "He ran." };
  const request: LookupRequest = { query: "ran", rect: { left: 10, top: 10, bottom: 30, width: 20 }, place };
  const posts = () => fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "POST");
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  const flush = () => act(async () => new Promise((r) => setTimeout(r, 0)));
  const render = async () => {
    await act(async () => root.render(createElement(MemoryRouter, null, createElement(DictionaryPopover, { request, lang: "en", onClose() {} }))));
    await flush();
  };
  const saveButton = () => el.querySelector<HTMLButtonElement>(".dict-save button")!;

  beforeAll(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  });
  beforeEach(() => {
    clearLookupCache();
    el = document.createElement("div");
    document.body.append(el);
    root = createRoot(el);
    fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") return json({ created: true, word: { seen_count: 1 } }, 201);
      return json(result());
    });
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    act(() => root.unmount());
    el.remove();
    vi.unstubAllGlobals();
  });

  it("sends nothing while it only shows a lookup, and the lookup carries no place", async () => {
    await render();
    expect(el.textContent).toContain("to move fast");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/dictionary/lookup");
    expect(String(url)).not.toMatch(/context|spine|work_id|page=/);
    expect((init as RequestInit | undefined)?.method ?? "GET").toBe("GET");
    expect(posts()).toHaveLength(0);
  });

  it("sends exactly one POST with the place when Save is pressed, and says Saved", async () => {
    await render();
    await act(async () => saveButton().click());
    await flush();
    expect(posts()).toHaveLength(1);
    const [url, init] = posts()[0];
    expect(url).toBe("/api/vocabulary");
    expect(JSON.parse((init as RequestInit).body as string)).toMatchObject({ word: "ran", lemma: "run", definition: "to move fast\nto operate", ...place });
    expect(el.querySelector(".dict-save [role=status]")?.textContent).toBe("Saved");
  });

  it("shows the seen count when the word was saved before", async () => {
    fetchMock.mockImplementation(async (_u: string, init?: RequestInit) => (init?.method === "POST" ? json({ created: false, word: { seen_count: 3 } }) : json(result())));
    await render();
    await act(async () => saveButton().click());
    await flush();
    expect(el.querySelector(".dict-save [role=status]")?.textContent).toBe("Saved again · seen 3×");
  });

  it("shows an error when offline, and sends one attempt only", async () => {
    fetchMock.mockImplementation(async (_u: string, init?: RequestInit) => {
      if (init?.method === "POST") throw new TypeError("Failed to fetch");
      return json(result());
    });
    await render();
    await act(async () => saveButton().click());
    await flush();
    expect(el.querySelector(".dict-save [role=alert]")?.textContent).toMatch(/offline/i);
    expect(el.querySelector(".dict-save [role=status]")).toBeNull();
    expect(posts()).toHaveLength(1);
  });
});
