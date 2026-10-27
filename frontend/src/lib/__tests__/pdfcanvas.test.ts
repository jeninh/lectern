// @vitest-environment jsdom
// A search hit's marks on a PDF page (pages/PageReader.tsx PdfCanvas, opened from "In books" in
// Cmd/Ctrl+K): only the current query's, never the previous one's while the page redraws, and
// never a superseded draw's that finishes late.
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import { PdfCanvas } from "../../pages/PageReader";

type Props = Parameters<typeof PdfCanvas>[0];

beforeAll(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  // jsdom has no canvas: drawing is the fake page's, and text measures one unit per character.
  HTMLCanvasElement.prototype.getContext = (() => ({ font: "", measureText: (s: string) => ({ width: s.length }) })) as never;
});

// One text item, "alpha beta", 100px wide from x = 100: "alpha" is marked at 100, "beta" at 160.
const ITEM = { str: "alpha beta", transform: [10, 0, 0, 10, 100, 200], width: 100 };
const draws: (() => void)[] = [];
const doc = {
  numPages: 1,
  getPage: async () => ({
    getViewport: () => ({ width: 600, height: 800, scale: 1, transform: [1, 0, 0, 1, 0, 0] }),
    getTextContent: async () => ({ items: [ITEM] }),
    // Each draw finishes when the test says so; cancelling does not settle it (pdf.js may have
    // finished already), so a superseded draw can still resolve after a newer one.
    render: () => {
      let done!: () => void;
      const promise = new Promise<void>((r) => (done = r));
      draws.push(done);
      return { promise, cancel() {} };
    },
  }),
} as unknown as Props["doc"];

const base: Omit<Props, "find"> = { doc, n: 1, count: 1, fit: "page", zoom: 1, stage: { w: 600, h: 800 }, dark: false, marks: [] };
const settle = () => act(async () => new Promise((r) => setTimeout(r, 0)));
/** Finish draw i (in the order they started) and let its marks land. */
const finish = (i: number) =>
  act(async () => {
    draws[i]();
    await new Promise((r) => setTimeout(r, 0));
  });

let el: HTMLDivElement;
let root: ReturnType<typeof createRoot>;
const show = (find: string) => act(() => root.render(createElement(PdfCanvas, { ...base, find })));
const marked = () => [...el.querySelectorAll<HTMLElement>(".pages-found mark")].map((m) => m.style.left);

afterEach(() => {
  act(() => root.unmount());
  draws.length = 0;
});

describe("PdfCanvas search marks", () => {
  it("drops the previous query's marks at once and keeps only the latest draw's", async () => {
    el = document.createElement("div");
    root = createRoot(el);
    await show("alpha");
    await settle();
    await finish(0);
    expect(marked()).toEqual(["100px"]);

    // Another hit on the same page: the old mark goes before the page has redrawn.
    await show("beta");
    await settle();
    expect(marked()).toEqual([]);

    // And back again before "beta" finished drawing; "beta" finishes last.
    await show("alpha");
    await settle();
    await finish(2);
    await finish(1);
    expect(marked()).toEqual(["100px"]);
  });
});
