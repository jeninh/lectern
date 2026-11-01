// @vitest-environment jsdom
// Home rows and the book-page toggle for Next in series and Want to read (issue #70).
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

const toast = vi.fn();
vi.mock("../store", () => ({ useStore: () => ({ toast }) }));
vi.mock("../../components/Cover", () => ({ Cover: () => null, tintStyle: () => ({}) }));

const { NextInSeriesRow, WantToReadRow, WantToReadButton } = await import("../../components/ReadingRows");

beforeAll(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});
let root: ReturnType<typeof createRoot> | null = null;
afterEach(() => {
  act(() => root?.unmount());
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});

const w = (id: number, title: string, extra = {}) => ({ id, title, author: "A", files: [], ...extra });
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });

async function mount(node: React.ReactElement, handler: (url: string, method: string, body?: string) => unknown) {
  const calls: { url: string; method: string; body?: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (u: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    calls.push({ url: u, method, body: init?.body as string | undefined });
    return json(handler(u, method, init?.body as string | undefined));
  }));
  const el = document.createElement("div");
  document.body.append(el);
  root = createRoot(el);
  await act(async () => root!.render(createElement(MemoryRouter, null, node)));
  await act(async () => {});
  return { el, calls };
}
const click = async (b: Element | null | undefined) => { await act(async () => (b as HTMLElement).click()); await act(async () => {}); };

describe("Next in series row", () => {
  it("shows each series' next book with its index, and nothing when empty", async () => {
    const { el } = await mount(createElement(NextInSeriesRow), () => ({ items: [w(2, "Book 2", { series: "Saga", series_index: 2 })] }));
    expect(el.textContent).toContain("Next in series");
    expect(el.textContent).toContain("Saga #2");
    act(() => root?.unmount());
    const empty = await mount(createElement(NextInSeriesRow), () => ({ items: [] }));
    expect(empty.el.textContent).toBe("");
  });
});

describe("Want to read row", () => {
  it("reorders by sending the whole new order, and removes one", async () => {
    let items = [w(1, "One"), w(2, "Two"), w(3, "Three")];
    const { el, calls } = await mount(createElement(WantToReadRow), (url, method, body) => {
      if (method === "PUT") items = (JSON.parse(body!).work_ids as number[]).map((id) => items.find((x) => x.id === id)!);
      if (method === "DELETE") items = items.filter((x) => `/api/want-to-read/${x.id}` !== url);
      return { items };
    });
    await click(el.querySelector('[aria-label="Move Three earlier"]'));
    expect(calls.at(-1)).toMatchObject({ method: "PUT", url: "/api/want-to-read/order", body: JSON.stringify({ work_ids: [1, 3, 2] }) });
    expect([...el.querySelectorAll("strong")].map((x) => x.textContent)).toEqual(["One", "Three", "Two"]);
    expect(el.querySelector<HTMLButtonElement>('[aria-label="Move One earlier"]')!.disabled).toBe(true);
    await click(el.querySelector('[aria-label="Remove Three from Want to read"]'));
    expect(calls.at(-1)).toMatchObject({ method: "DELETE", url: "/api/want-to-read/3" });
    expect([...el.querySelectorAll("strong")].map((x) => x.textContent)).toEqual(["One", "Two"]);
  });
});

describe("Want to read button", () => {
  it("adds, then removes", async () => {
    let items: ReturnType<typeof w>[] = [];
    const { el, calls } = await mount(createElement(WantToReadButton, { workId: 9 }), (_u, method) => {
      if (method === "POST") items = [w(9, "Nine")];
      if (method === "DELETE") items = [];
      return { items };
    });
    expect(el.textContent).toBe("Want to read");
    await click(el.querySelector("button"));
    expect(calls.at(-1)).toMatchObject({ method: "POST", body: JSON.stringify({ work_id: 9 }) });
    expect(el.textContent).toBe("Remove from Want to read");
    await click(el.querySelector("button"));
    expect(calls.at(-1)).toMatchObject({ method: "DELETE", url: "/api/want-to-read/9" });
    expect(el.textContent).toBe("Want to read");
  });
});
