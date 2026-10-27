// @vitest-environment jsdom
// "In books" in Cmd/Ctrl+K (components/CommandPalette.tsx) when the server refuses a search:
// a rate limit or a query that is too long says so, instead of looking like "no matches".
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

vi.mock("../store", () => {
  const store = {
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
  };
  return { useStore: () => store };
});
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

/** Opens the palette, types `q`, and returns the "In books" note once the search answered. */
async function noteFor(q: string, status: number, body: unknown): Promise<string | null> {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url.startsWith("/api/search/text")
        ? new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } })
        : new Response(JSON.stringify({ devices: [] }), { status: 200 }),
    ),
  );
  const el = document.createElement("div");
  document.body.append(el);
  root = createRoot(el);
  await act(async () => root!.render(createElement(MemoryRouter, null, createElement(CommandPalette))));
  const input = document.querySelector<HTMLInputElement>('[role="combobox"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, q);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  // Past the debounce, and the answer.
  await act(async () => new Promise((r) => setTimeout(r, 400)));
  const note = [...document.querySelectorAll(".palette-note")].find((n) => n.textContent?.startsWith("In books"));
  return note?.textContent?.replace(/^In books/, "") ?? null;
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
