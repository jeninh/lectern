// @vitest-environment jsdom
// The highlights page (pages/Highlights.tsx, issue #72): your highlights searched and filtered,
// the matched words marked, each opening the reader at its passage, and the Markdown export of
// exactly the results shown.
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

const store = vi.hoisted(() => ({
  works: [{ id: 5, title: "The Gift of the Magi", author: "O. Henry", files: [{ id: 1, media_type: "application/epub+zip" }] }],
}));
vi.mock("../store", () => ({ useStore: () => store }));

const { HighlightsPage } = await import("../../pages/Highlights");

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
const SUMMARY = {
  works: [
    { work_id: 5, title: "The Gift of the Magi", author: "O. Henry", tags: ["christmas"], count: 2 },
    { work_id: 8, title: "Moby-Dick", author: "Herman Melville", tags: [], count: 1 },
  ],
  total: 3,
  export_url: "/api/annotations.md",
};
const HIT = {
  work_id: 5,
  title: "The Gift of the Magi",
  author: "O. Henry",
  annotation: {
    id: 9, key: "k", text: "Three times Della counted it", note: "Remember the café", chapter: "I", color: null,
    page: null, pageno: 3, created: "2026-10-01 09:30:00", updated: "2026-10-01 09:30:00", device: "Kindle",
    file: { id: 1, media_type: "application/epub+zip" }, location: { spine_index: 2, char: 285, length: 28 }, sentence_n: 4,
  },
  text_parts: [["Three times Della counted it", false]],
  note_parts: [["Remember the ", false], ["café", true]],
  field: "note",
  snippet: null,
};

async function settle() {
  await act(async () => new Promise((r) => setTimeout(r, 350)));
}

async function open(url: string, search: (u: URL) => Response) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (u: string) => {
      calls.push(u);
      if (u === "/api/annotations") return json(200, SUMMARY);
      return search(new URL(u, "http://x"));
    }),
  );
  const el = document.createElement("div");
  document.body.append(el);
  root = createRoot(el);
  await act(async () => root!.render(createElement(MemoryRouter, { initialEntries: [url] }, createElement(HighlightsPage))));
  await settle();
  return calls;
}

function change(el: HTMLInputElement | HTMLSelectElement, value: string) {
  const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
}

describe("the highlights page", () => {
  it("shows results with the words marked, opening the reader at the passage", async () => {
    await open("/app/highlights?q=cafe", () => json(200, { results: [HIT], total: 1, truncated: false }));
    const item = document.querySelector(".highlight-results .highlight")!;
    expect(item.querySelector(".highlight-note mark")?.textContent).toBe("café");
    expect(item.querySelector("blockquote")?.textContent).toBe("Three times Della counted it");
    const go = [...item.querySelectorAll("a")].find((a) => a.textContent === "Open in reader")!;
    expect(go.getAttribute("href")).toBe("/app/reader/5?at=2:285&len=28");
    expect(document.querySelector<HTMLInputElement>("#hl-q")!.value).toBe("cafe");
    expect(document.querySelector('[role="status"]')?.textContent).toBe("1 of 3 highlights");
  });

  it("searches and filters through the API, and exports exactly what it shows", async () => {
    const calls = await open("/app/highlights", () => json(200, { results: [HIT], total: 1, truncated: false }));
    expect(calls).toContain("/api/annotations/search?limit=200&partial=false");
    expect(document.querySelector<HTMLAnchorElement>("a[download]")!.getAttribute("href")).toBe("/api/annotations.md");
    // Filters offered from your own books: their authors and tags.
    const tags = [...document.querySelectorAll<HTMLOptionElement>("#hl-tag option")].map((o) => o.value);
    expect(tags).toEqual(["", "christmas"]);

    await act(async () => change(document.querySelector<HTMLSelectElement>("#hl-author")!, "O. Henry"));
    await act(async () => change(document.querySelector<HTMLInputElement>("#hl-q")!, "note: café"));
    await settle();
    await settle();
    const want = "q=note%3A+caf%C3%A9&author=O.+Henry";
    expect(calls).toContain(`/api/annotations/search?${want}&limit=200&partial=false`);
    const exp = document.querySelector<HTMLAnchorElement>("a[download]")!;
    expect(exp.getAttribute("href")).toBe(`/api/annotations.md?${want}`);
    expect(exp.textContent).toContain("Export these as Markdown");
  });

  it("says why a search is refused, and when nothing matches", async () => {
    await open("/app/highlights?q=whale", () => json(429, { detail: "too_many_searches", message: "Too many searches; wait a moment." }));
    expect(document.querySelector('[role="alert"]')?.textContent).toBe("Too many searches; wait a moment.");
    act(() => root?.unmount());
    document.body.replaceChildren();
    await open("/app/highlights?q=whale", () => json(200, { results: [], total: 0, truncated: false }));
    expect(document.body.textContent).toContain("No highlights match “whale”");
    expect(document.querySelector("a[download]")).toBeNull();
  });
});
