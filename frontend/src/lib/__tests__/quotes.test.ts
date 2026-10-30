// @vitest-environment jsdom
// The quotes feed (the Highlights page's Quotes view) and Home's highlight of the day
// (components/QuoteFeed.tsx, issue #83).
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

const store = vi.hoisted(() => ({
  works: [{ id: 5, title: "The Gift of the Magi", author: "O. Henry", cover_url: null, cover_tint: null, files: [{ id: 1, media_type: "application/epub+zip" }] }],
  shelves: [{ id: 3, name: "Christmas", path: ["Stories", "Christmas"] }],
  // 23:30 local time on 28 October: "today" is the person's date, not UTC's.
  now: new Date(2026, 9, 28, 23, 30).getTime(),
}));
vi.mock("../store", () => ({ useStore: () => store }));

const { HighlightsPage } = await import("../../pages/Highlights");
const { QuoteOfTheDayCard } = await import("../../components/QuoteFeed");
const { localDay, quotesQuery } = await import("../quotes");

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
  works: [{ work_id: 5, title: "The Gift of the Magi", author: "O. Henry", tags: ["christmas"], count: 2 }],
  total: 2,
  export_url: "/api/annotations.md",
};
const quote = (id: number, text: string, extra: Record<string, unknown> = {}) => ({
  work_id: 5,
  title: "The Gift of the Magi",
  author: "O. Henry",
  annotation: {
    id, key: `k${id}`, text, note: null, chapter: "I", color: null, page: null, pageno: 3,
    created: "2026-10-01 09:30:00", updated: "2026-10-01 09:30:00", device: "Kindle",
    file: { id: 1, media_type: "application/epub+zip" }, location: { spine_index: 2, char: 285 + id, length: 28 },
    sentence_n: 4, favourite: false, hidden: false, ...extra,
  },
});
const A = quote(9, "Three times Della counted it", { note: "Remember this" });
const B = quote(10, "So Della did it");

type Call = { url: string; method: string; body: unknown };

async function render(el: ReturnType<typeof createElement>, url: string, respond: (c: Call) => Response) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (u: string, init?: RequestInit) => {
      const c = { url: u, method: init?.method ?? "GET", body: init?.body ? JSON.parse(init.body as string) : undefined };
      calls.push(c);
      return respond(c);
    }),
  );
  const host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  await act(async () => root!.render(createElement(MemoryRouter, { initialEntries: [url] }, el)));
  await act(async () => new Promise((r) => setTimeout(r, 0)));
  return calls;
}

const click = async (el: Element) => act(async () => (el as HTMLElement).click());
const button = (label: string, scope: ParentNode = document) =>
  [...scope.querySelectorAll("button")].find((b) => b.textContent?.trim() === label || b.getAttribute("aria-label") === label)!;

describe("quotes helpers", () => {
  it("builds the feed's query without defaults, and a seed only for a shuffle", () => {
    expect(quotesQuery({ order: "newest", show: "all", seed: "abc" })).toBe("");
    expect(quotesQuery({ work: "5", shelf: "3", order: "shuffle", seed: "abc", show: "favourites" })).toBe("work=5&shelf=3&order=shuffle&seed=abc&show=favourites");
  });

  it("takes the person's local date, not UTC's", () => {
    expect(localDay(new Date(2026, 9, 28, 23, 59))).toBe("2026-10-28");
    expect(localDay(new Date(2026, 0, 1, 0, 1))).toBe("2026-01-01");
  });
});

describe("the quotes feed", () => {
  const feed = (results: unknown[], daily = true) => json(200, { results, total: results.length, daily });

  it("shows your highlights as cards that open the passage, and filters by shelf", async () => {
    const calls = await render(createElement(HighlightsPage), "/app/highlights?view=quotes", (c) =>
      c.url === "/api/annotations" ? json(200, SUMMARY) : feed([A, B]),
    );
    expect(calls.map((c) => c.url)).toContain("/api/quotes?limit=30");
    // The search list is not loaded behind the feed.
    expect(calls.some((c) => c.url.startsWith("/api/annotations/search"))).toBe(false);
    const cards = [...document.querySelectorAll(".quote-feed .quote-card")];
    expect(cards.map((c) => c.querySelector("blockquote")?.textContent)).toEqual(["Three times Della counted it", "So Della did it"]);
    expect(cards[0].querySelector(".highlight-note")?.textContent).toBe("Note: Remember this");
    expect(cards[0].querySelector(".quote-cover .cover")).not.toBeNull();
    const open = [...cards[0].querySelectorAll("a")].find((a) => a.textContent === "Open in reader")!;
    expect(open.getAttribute("href")).toBe("/app/reader/5?at=2:294&len=28");
    expect(document.querySelector('[role="status"]')?.textContent).toBe("2 quotes");

    const shelf = document.querySelector<HTMLSelectElement>("#qt-shelf")!;
    expect([...shelf.options].map((o) => o.textContent)).toEqual(["All shelves", "Stories / Christmas"]);
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(shelf, "3");
      shelf.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(calls.map((c) => c.url)).toContain("/api/quotes?shelf=3&limit=30");
  });

  it("shuffles with a seed, favourites in place, and takes a hidden quote out", async () => {
    let shown = [A, B];
    const calls = await render(createElement(HighlightsPage), "/app/highlights?view=quotes", (c) => {
      if (c.url === "/api/annotations") return json(200, SUMMARY);
      if (c.url.endsWith("/mark")) {
        const id = Number(c.url.split("/")[3]);
        const q = shown.find((x) => x.annotation.id === id)!;
        return json(200, { ...q, annotation: { ...q.annotation, ...(c.body as object) } });
      }
      return feed(shown);
    });
    await click(button("Shuffle"));
    const shuffle = calls.map((c) => c.url).find((u) => u.includes("order=shuffle"));
    expect(shuffle).toMatch(/^\/api\/quotes\?order=shuffle&seed=[a-z0-9]+&limit=30$/);
    expect(button("Shuffle again")).toBeTruthy();

    const first = document.querySelector(".quote-card")!;
    await click(button("Add to favourites", first));
    expect(calls.at(-1)).toEqual({ url: "/api/annotations/9/mark", method: "POST", body: { favourite: true } });
    expect(button("Remove from favourites", first).getAttribute("aria-pressed")).toBe("true");

    shown = [A, B];
    const second = document.querySelectorAll(".quote-card")[1];
    await click(button("Hide", second));
    expect(calls.at(-1)).toEqual({ url: "/api/annotations/10/mark", method: "POST", body: { hidden: true } });
    expect([...document.querySelectorAll(".quote-card blockquote")].map((b) => b.textContent)).toEqual(["Three times Della counted it"]);
    expect(document.querySelector('[role="status"]')?.textContent).toBe("1 quote");
  });

  it("turns the highlight of the day back on from the feed", async () => {
    const calls = await render(createElement(HighlightsPage), "/app/highlights?view=quotes", (c) => {
      if (c.url === "/api/annotations") return json(200, SUMMARY);
      if (c.url === "/api/quotes/settings") return json(200, { daily: (c.body as { daily: boolean }).daily });
      return feed([A], false);
    });
    const box = document.querySelector<HTMLInputElement>(".quotes-daily input")!;
    expect(box.checked).toBe(false);
    await click(box);
    expect(calls.at(-1)).toEqual({ url: "/api/quotes/settings", method: "POST", body: { daily: true } });
    expect(box.checked).toBe(true);
  });
});

describe("the highlight of the day on Home", () => {
  it("asks for your local day's quote, opens its passage, and can be turned off", async () => {
    const calls = await render(createElement(QuoteOfTheDayCard), "/app", (c) =>
      c.url.startsWith("/api/quotes/today") ? json(200, { enabled: true, day: "2026-10-28", quote: A }) : json(200, { daily: false }),
    );
    expect(calls[0].url).toBe("/api/quotes/today?day=2026-10-28");
    const card = document.querySelector(".quote-today")!;
    expect(card.querySelector("h2")?.textContent).toBe("Highlight of the day");
    expect(card.querySelector("blockquote")?.textContent).toBe("Three times Della counted it");
    expect(card.textContent).toContain("The Gift of the Magi");
    const open = [...card.querySelectorAll("a")].find((a) => a.textContent === "Open in reader")!;
    expect(open.getAttribute("href")).toBe("/app/reader/5?at=2:294&len=28");

    await click(button("Turn off", card));
    expect(calls.at(-1)).toEqual({ url: "/api/quotes/settings", method: "POST", body: { daily: false } });
    expect(document.querySelector(".quote-today")).toBeNull();
  });

  it("shows nothing when turned off, when there is none, or offline", async () => {
    await render(createElement(QuoteOfTheDayCard), "/app", () => json(200, { enabled: false, day: "2026-10-28", quote: null }));
    expect(document.querySelector(".quote-today")).toBeNull();
    act(() => root?.unmount());
    await render(createElement(QuoteOfTheDayCard), "/app", () => {
      throw new TypeError("Failed to fetch");
    });
    expect(document.querySelector(".quote-today")).toBeNull();
  });
});
