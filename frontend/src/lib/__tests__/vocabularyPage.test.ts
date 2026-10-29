// @vitest-environment jsdom
// The vocabulary page (pages/Vocabulary.tsx, issue #80): filters go to the API, saved words are
// edited and deleted in place, exports carry the filters, and "Open in book" links to the passage.
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

const store = vi.hoisted(() => ({
  works: [{ id: 5, title: "The Gift of the Magi", author: "O. Henry", files: [{ id: 1, media_type: "application/epub+zip" }] }],
}));
vi.mock("../store", () => ({ useStore: () => store }));

const { VocabularyPage } = await import("../../pages/Vocabulary");

beforeAll(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

let root: ReturnType<typeof createRoot> | null = null;
afterEach(() => {
  act(() => root?.unmount());
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const WORD = {
  id: 7, word: "chevaux", lemma: "cheval", lang: "fr", to: "en", definition: "horse", note: null, pos: "noun", dictionary: "FreeDict",
  context: "Les chevaux courent.", book: { id: 5, title: "The Gift of the Magi", author: "O. Henry" },
  location: { spine_index: 2, char: 285, length: 7 }, page: null, file_id: null, seen_count: 3,
  created_at: "2026-10-01T09:30:00+00:00", last_seen_at: "2026-10-02T09:30:00+00:00", updated_at: "2026-10-02T09:30:00+00:00",
};
const LISTING = {
  words: [WORD],
  total: 1,
  facets: { languages: [{ lang: "fr", count: 1 }, { lang: "en", count: 4 }], works: [{ id: 5, title: "The Gift of the Magi", author: "O. Henry", count: 1 }] },
};

async function open(url: string, handler: (_u: string, init?: RequestInit) => Response = () => json(200, LISTING)) {
  const calls: { url: string; method: string; body?: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (u: string, init?: RequestInit) => {
      calls.push({ url: u, method: init?.method ?? "GET", body: init?.body as string | undefined });
      return handler(u, init);
    }),
  );
  const el = document.createElement("div");
  document.body.append(el);
  root = createRoot(el);
  await act(async () => root!.render(createElement(MemoryRouter, { initialEntries: [url] }, createElement(VocabularyPage))));
  await act(async () => new Promise((r) => setTimeout(r, 20)));
  return calls;
}

function change(el: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement, value: string) {
  const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
}
const button = (label: string) => [...document.querySelectorAll("button")].find((b) => b.textContent === label)!;
const click = (el: Element) => act(async () => { el.dispatchEvent(new MouseEvent("click", { bubbles: true })); await new Promise((r) => setTimeout(r, 20)); });

describe("the vocabulary page", () => {
  it("shows a word with its context, and opens the book at the passage", async () => {
    await open("/app/vocabulary");
    const item = document.querySelector(".vocab-item")!;
    expect(item.textContent).toContain("chevaux");
    expect(item.textContent).toContain("cheval");
    expect(item.textContent).toContain("fr → en");
    expect(item.textContent).toContain("seen 3×");
    expect(item.querySelector("blockquote")?.textContent).toBe("Les chevaux courent.");
    const go = [...item.querySelectorAll("a")].find((a) => a.textContent === "Open in book")!;
    expect(go.getAttribute("href")).toBe("/app/reader/5?at=2:285&len=7");
  });

  it("sends the filters to the API and to both export links", async () => {
    const calls = await open("/app/vocabulary");
    expect(calls[0].url).toBe("/api/vocabulary?offset=0");
    expect(document.querySelector("a[download]")!.getAttribute("href")).toBe("/api/vocabulary/export?format=anki");
    await act(async () => change(document.querySelector<HTMLSelectElement>("#vocab-work")!, "5"));
    await act(async () => change(document.querySelector<HTMLSelectElement>("#vocab-lang")!, "fr"));
    const dates = document.querySelectorAll<HTMLInputElement>('input[type="date"]');
    await act(async () => change(dates[0], "2026-10-01"));
    await act(async () => change(dates[1], "2026-10-31"));
    const want = "work=5&lang=fr&since=2026-10-01&until=2026-10-31";
    expect(calls.map((c) => c.url)).toContain(`/api/vocabulary?${want}&offset=0`);
    const links = [...document.querySelectorAll<HTMLAnchorElement>("a[download]")];
    expect(links.map((a) => [a.textContent?.trim(), a.getAttribute("href")])).toEqual([
      ["Export for Anki", `/api/vocabulary/export?format=anki&${want}`],
      ["Export CSV", `/api/vocabulary/export?format=csv&${want}`],
    ]);
  });

  it("edits the definition and note with PATCH, and deletes with DELETE", async () => {
    const edited = { ...WORD, definition: "horses", note: "plural" };
    const calls = await open("/app/vocabulary", (_u, init) => {
      if (init?.method === "PATCH") return json(200, { word: edited });
      if (init?.method === "DELETE") return json(200, { removed: 7 });
      return json(200, LISTING);
    });
    await click(button("Edit"));
    await act(async () => {
      const [def, note] = document.querySelectorAll<HTMLTextAreaElement>(".vocab-edit textarea");
      change(def, "horses");
      change(note, "plural");
    });
    await click(button("Save"));
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect(patch.url).toBe("/api/vocabulary/7");
    expect(JSON.parse(patch.body!)).toEqual({ definition: "horses", note: "plural" });
    expect(document.querySelector(".vocab-def")?.textContent).toBe("horses");
    expect(document.querySelector(".vocab-note")?.textContent).toContain("plural");

    await click(button("Delete"));
    expect(calls.find((c) => c.method === "DELETE")!.url).toBe("/api/vocabulary/7");
    expect(document.querySelector(".vocab-item")).toBeNull();
  });
});
