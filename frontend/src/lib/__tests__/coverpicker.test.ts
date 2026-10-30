// @vitest-environment jsdom
// The cover tab's pickers (issue #85), rendered for real: the candidate grid as a keyboard radio
// group that sets the draft, its debounce and "More colours" paging, and the history list's
// revert for each kind of entry.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const env = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("../api", async (orig) => ({ ...(await orig<typeof import("../api")>()), get: env.get }));

import { CandidateGrid, CoverHistory } from "../../components/CoverPicker";
import type { CoverDraft } from "../editor";
import type { CoverCandidate, CoverHistoryEntry } from "../types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;
beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  env.get.mockReset();
});

const cand = (seed: number): CoverCandidate => ({ style: (["classic", "modern", "minimal"] as const)[seed % 3], seed, font: "display", palette: `Palette ${seed}`, url: `/c/${seed}.jpg` });
const page = (offset: number, count = 6) => ({ fonts: ["display", "text", "noto"], styles: ["classic", "modern", "minimal"], candidates: Array.from({ length: count }, (_, i) => cand(offset + i)) });

/** fetch that answers each candidates request from its offset, recording URLs and abort state. */
function stubFetch() {
  const calls: { url: URL; signal: AbortSignal }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string, init: RequestInit) => {
    const url = new URL(input, "http://x");
    calls.push({ url, signal: init.signal as AbortSignal });
    return { ok: true, status: 200, json: async () => page(Number(url.searchParams.get("offset"))) } as Response;
  }));
  return calls;
}
const tick = async (ms: number) => act(async () => void (await vi.advanceTimersByTimeAsync(ms)));
const radios = () => [...host.querySelectorAll<HTMLButtonElement>('[role="radio"]')];

function grid(props: { title?: string; cover?: CoverDraft | null; onPick?: (c: CoverDraft) => void } = {}) {
  const render = (p: typeof props) => act(() => root.render(createElement(CandidateGrid, { workId: 9, title: p.title ?? "Dune", author: "Frank Herbert", cover: p.cover ?? null, onPick: p.onPick ?? (() => {}) })));
  render(props);
  return render;
}

describe("candidate grid", () => {
  beforeEach(() => vi.useFakeTimers());

  it("is a radiogroup of lazy images; arrows move focus and pick, wrapping at the ends", async () => {
    stubFetch();
    const picked: CoverDraft[] = [];
    const render = grid({ onPick: (c) => picked.push(c) });
    await tick(0);
    expect(host.querySelector('[role="radiogroup"]')).not.toBeNull();
    expect(radios()).toHaveLength(6);
    expect(host.querySelectorAll('img[loading="lazy"]')).toHaveLength(6);
    // Nothing chosen yet: the first radio is the one tab stop.
    expect(radios().map((r) => r.tabIndex)).toEqual([0, -1, -1, -1, -1, -1]);
    const press = (key: string) => act(() => void radios().find((r) => r === document.activeElement)!.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true })));
    radios()[0].focus();
    press("ArrowLeft"); // wraps to the last
    expect(document.activeElement).toBe(radios()[5]);
    expect(picked.at(-1)).toEqual({ action: "generate", style: "minimal", seed: 5, font: "display" });
    press("ArrowRight"); // and back to the first
    expect(picked.at(-1)).toEqual({ action: "generate", style: "classic", seed: 0, font: "display" });
    // With the draft on candidate 2, only that radio is checked and focusable.
    render({ cover: { action: "generate", style: "minimal", seed: 2, font: "display" } });
    expect(radios().map((r) => r.getAttribute("aria-checked"))).toEqual(["false", "false", "true", "false", "false", "false"]);
    expect(radios().map((r) => r.tabIndex)).toEqual([-1, -1, 0, -1, -1, -1]);
  });

  it("clicking a design sets the draft to generate with its style, seed, and font", async () => {
    stubFetch();
    const picked: CoverDraft[] = [];
    grid({ onPick: (c) => picked.push(c) });
    await tick(0);
    act(() => radios()[4].click());
    expect(picked).toEqual([{ action: "generate", style: "modern", seed: 4, font: "display" }]);
  });

  it("loads the first page at once, then waits 400 ms after typing and aborts the stale request", async () => {
    const calls = stubFetch();
    const render = grid({ title: "D" });
    await tick(0);
    expect(calls).toHaveLength(1);
    expect(calls[0].url.searchParams.get("offset")).toBe("0");
    expect(calls[0].url.searchParams.get("count")).toBe("6");
    render({ title: "Du" });
    await tick(200);
    render({ title: "Dun" });
    await tick(399);
    expect(calls).toHaveLength(1); // still typing
    await tick(1);
    expect(calls).toHaveLength(2);
    expect(calls[1].url.searchParams.get("title")).toBe("Dun");
    // Typing again while that request is open aborts it.
    render({ title: "Dune" });
    await tick(0);
    expect(calls[1].signal.aborted).toBe(true);
  });

  it("'More colours' asks for the next offset and appends; typing starts over at offset 0", async () => {
    const calls = stubFetch();
    const render = grid();
    await tick(0);
    const more = () => [...host.querySelectorAll("button")].find((b) => b.textContent === "More colours")!;
    await act(async () => more().click());
    await tick(0);
    expect(calls.at(-1)!.url.searchParams.get("offset")).toBe("6");
    expect(radios()).toHaveLength(12);
    await act(async () => more().click());
    await tick(0);
    expect(calls.at(-1)!.url.searchParams.get("offset")).toBe("12");
    expect(radios()).toHaveLength(18);
    render({ title: "Children of Dune" });
    await tick(400);
    expect(calls.at(-1)!.url.searchParams.get("offset")).toBe("0");
    expect(radios()).toHaveLength(6);
  });

  it("offers the fonts the server lists and reloads in the chosen one", async () => {
    const calls = stubFetch();
    grid();
    await tick(0);
    expect(calls[0].url.searchParams.get("font")).toBe("display");
    const select = host.querySelector("select")!;
    expect([...select.options].map((o) => o.value)).toEqual(["display", "text", "noto"]);
    await act(async () => {
      select.value = "noto";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await tick(400);
    expect(calls.at(-1)!.url.searchParams.get("font")).toBe("noto");
  });
});

describe("cover history", () => {
  const entries: CoverHistoryEntry[] = [
    { current: true, revision_id: null, kind: "image", url: "/library/works/9/cover.jpg?v=4", source: "upload" },
    { current: false, revision_id: 31, kind: "generated", url: "/g.jpg", style: "modern", seed: 7, font: "noto", replaced_at: "2026-10-20T10:00:00Z" },
    { current: false, revision_id: 22, kind: "image", url: "/api/works/9/cover/history/22.jpg", source: "embedded", replaced_at: "2026-10-10T10:00:00Z" },
  ];

  it("lists the entries, says it covers 30 days, and reverts each kind with the right patch", async () => {
    env.get.mockResolvedValue({ entries });
    const reverted: CoverDraft[] = [];
    await act(async () => root.render(createElement(CoverHistory, { workId: 9, draft: null, onRevert: (c) => reverted.push(c) })));
    expect(env.get).toHaveBeenCalledWith("/api/works/9/cover/history");
    expect(host.textContent).toContain("last 30 days");
    expect(host.querySelectorAll("li")).toHaveLength(3);
    expect(host.querySelectorAll("li img")[0].getAttribute("loading")).toBe("lazy");
    const buttons = [...host.querySelectorAll<HTMLButtonElement>("li button")];
    expect(buttons).toHaveLength(2); // the current cover has no revert
    act(() => buttons[0].click());
    act(() => buttons[1].click());
    expect(reverted).toEqual([
      { action: "generate", style: "modern", seed: 7, font: "noto" },
      { action: "revision", revision_id: 22, preview: "/api/works/9/cover/history/22.jpg" },
    ]);
  });

  it("marks the entry the draft is already reverting to", async () => {
    env.get.mockResolvedValue({ entries });
    await act(async () => root.render(createElement(CoverHistory, { workId: 9, draft: { action: "revision", revision_id: 22 }, onRevert: () => {} })));
    expect([...host.querySelectorAll("li button")].map((b) => b.getAttribute("aria-pressed"))).toEqual(["false", "true"]);
  });
});
