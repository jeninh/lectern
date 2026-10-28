// @vitest-environment jsdom
// "In books" in Cmd/Ctrl+K (components/CommandPalette.tsx) when the server refuses a search:
// a rate limit or a query that is too long says so, instead of looking like "no matches".
// "Highlights": your own highlights and notes, opening the reader at the passage (issue #72).
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

const store = vi.hoisted(() => ({
    paletteOpen: true,
    setPaletteOpen: () => {},
    works: [],
    shelves: [],
    me: { user: { id: 1, username: "r", display_name: "R", role: "reader" } },
    resolvedTheme: "light",
    setTheme: () => {},
    setHelpOpen: () => {},
    canUpload: false,
    canEdit: false,
    pickBooks: () => {},
    pickFolder: () => {},
    imports: [],
    setReviewOpen: () => {},
    openEditor: () => {},
  } as Record<string, unknown>));
vi.mock("../store", () => ({ useStore: () => store }));
vi.mock("../player", () => ({ usePlayer: () => ({ work: null, playing: false }) }));

const { CommandPalette } = await import("../../components/CommandPalette");

beforeAll(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  Element.prototype.scrollIntoView = () => {}; // not in jsdom
});

let root: ReturnType<typeof createRoot> | null = null;
afterEach(() => {
  act(() => root?.unmount());
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const NONE = { results: [], total: 0, truncated: false };

function Where() {
  const loc = useLocation();
  return createElement("output", { id: "where" }, loc.pathname + loc.search);
}

/** Opens the palette with the server answering `answers` (by path prefix) and types `q`. */
async function typeIn(q: string, answers: Record<string, () => Response>): Promise<void> {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const key = Object.keys(answers).find((k) => url.startsWith(k));
      return key ? answers[key]() : json(200, { devices: [] });
    }),
  );
  const el = document.createElement("div");
  document.body.append(el);
  root = createRoot(el);
  const routes = createElement(Routes, null, createElement(Route, { path: "*", element: createElement(Where) }));
  await act(async () => root!.render(createElement(MemoryRouter, null, createElement(CommandPalette), routes)));
  const input = document.querySelector<HTMLInputElement>('[role="combobox"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, q);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  // Past the debounce, and the answer.
  await act(async () => new Promise((r) => setTimeout(r, 400)));
}

function noteOf(group: string): string | null {
  const note = [...document.querySelectorAll(".palette-note")].find((n) => n.querySelector(".group")?.textContent === group);
  return note?.textContent?.slice(group.length) ?? null;
}

/** Opens the palette, types `q`, and returns the "In books" note once the search answered. */
async function noteFor(q: string, status: number, body: unknown): Promise<string | null> {
  await typeIn(q, { "/api/search/text": () => json(status, body), "/api/annotations/search": () => json(200, NONE) });
  return noteOf("In books");
}

describe("In books, when the server refuses", () => {
  it("shows the rate limit's message", async () => {
    expect(await noteFor("whale", 429, { detail: "too_many_searches", message: "Too many searches; wait a moment." })).toBe("Too many searches; wait a moment.");
  });

  it("shows why a query is refused", async () => {
    expect(await noteFor("a very long query", 422, { detail: "query_too_long", message: "Searches are at most 200 characters." })).toBe("Searches are at most 200 characters.");
  });

  it("says nothing extra when the search simply finds nothing", async () => {
    expect(await noteFor("whale", 200, { results: [], too_broad: false })).toBeNull();
  });
});

describe("Highlights", () => {
  const hit = {
    work_id: 5,
    title: "The Gift of the Magi",
    author: "O. Henry",
    annotation: {
      id: 9, key: "k", text: "Three times Della counted it", note: "Remember the café", chapter: null, color: null,
      page: null, pageno: 3, created: "2026-10-01 09:30:00", updated: "2026-10-01 09:30:00", device: "Kindle",
      file: { id: 1, media_type: "application/epub+zip" }, location: { spine_index: 2, char: 285, length: 28 }, sentence_n: 4,
    },
    text_parts: [["Three times Della counted it", false]],
    note_parts: [["Remember the ", false], ["café", true]],
    field: "note",
    snippet: { parts: [["Remember the ", false], ["café", true]], before: false, after: false },
  };
  const group = () => [...document.querySelectorAll('[role="group"]')].find((g) => g.querySelector(".group")?.textContent === "Highlights");

  it("lists your matching highlights above In books and opens the reader at the passage", async () => {
    store.works = [{ id: 5, title: "The Gift of the Magi", author: "O. Henry", files: [{ id: 1, media_type: "application/epub+zip" }] }];
    try {
      await typeIn("cafe", {
        "/api/annotations/search": () => json(200, { results: [hit], total: 1, truncated: false }),
        "/api/search/text": () => json(200, { results: [{ work_id: 5, title: "The Gift of the Magi", author: "O. Henry", format: "epub", file_id: 1, match: "cafe", snippet: { parts: [["cafe", true]], before: false, after: false }, spine_index: 0, char: 0, length: 4 }], too_broad: false }),
      });
      const names = [...document.querySelectorAll('[role="group"] > .group')].map((g) => g.textContent);
      expect(names.indexOf("Highlights")).toBeGreaterThan(-1);
      expect(names.indexOf("Highlights")).toBeLessThan(names.indexOf("In books"));
      const option = group()!.querySelector<HTMLElement>('[role="option"]')!;
      expect(option.querySelector("mark")?.textContent).toBe("café");
      expect(option.textContent).toContain("· note");
      await act(async () => option.click());
      expect(document.querySelector("#where")?.textContent).toBe("/app/reader/5?at=2:285&len=28");
    } finally {
      store.works = [];
    }
  });

  it("says why when the server refuses, once when both searches are refused alike", async () => {
    const limited = () => json(429, { detail: "too_many_searches", message: "Too many searches; wait a moment." });
    await typeIn("whale", { "/api/annotations/search": limited, "/api/search/text": () => json(200, { results: [], too_broad: false }) });
    expect(noteOf("Highlights")).toBe("Too many searches; wait a moment.");
    expect(group()).toBeUndefined();
    act(() => root?.unmount());
    document.body.replaceChildren();
    await typeIn("whale", { "/api/annotations/search": limited, "/api/search/text": limited });
    expect(noteOf("In books")).toBe("Too many searches; wait a moment.");
    expect(noteOf("Highlights")).toBeNull();
  });

  it("asks for highlights with the query, a finished last word not as a prefix", async () => {
    await typeIn("della ", { "/api/annotations/search": () => json(200, NONE), "/api/search/text": () => json(200, { results: [], too_broad: false }) });
    const urls = (fetch as unknown as { mock: { calls: [string][] } }).mock.calls.map((c) => c[0]);
    expect(urls).toContain("/api/annotations/search?q=della&limit=5&partial=false");
  });
});
