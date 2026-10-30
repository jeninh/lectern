// @vitest-environment jsdom
// Your private rating, notes, and read-throughs (components/BookReview.tsx, issue #84): the
// book detail page's section and the reader's "Rate it?" at the end of a book.
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { localToday, notesTokens, readsBody, starFill, starsLabel } from "../reviews";
import type { BookReview } from "../types";

const store = vi.hoisted(() => ({ toast: vi.fn() }));
vi.mock("../store", () => ({ useStore: () => store }));

const { BookReview: Section, RatePrompt } = await import("../../components/BookReview");

beforeAll(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

let root: ReturnType<typeof createRoot> | null = null;
afterEach(() => {
  act(() => root?.unmount());
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  store.toast.mockReset();
});

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const FINISHED: BookReview = {
  work_id: 7, rating: null, notes: "", corrected: false, finished: true, dismissed: false, prompt: true, updated_at: null,
  read_throughs: [{ id: null, started_on: "2026-01-05", finished_on: "2026-02-01", derived: true }],
};

/** A stand-in for the review API: answers GET with `state`, and records what is sent. */
function server(initial: BookReview, reply?: (method: string, body: Record<string, unknown>) => Response | null) {
  let state = initial;
  const sent: { method: string; url: string; body: Record<string, unknown> }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : {};
      if (method !== "GET") sent.push({ method, url, body });
      const custom = reply?.(method, body);
      if (custom) return custom;
      if (method === "PUT") {
        state = { ...state, ...("rating" in body ? { rating: body.rating as number | null } : {}), ...("notes" in body ? { notes: body.notes as string } : {}) };
        if ("read_throughs" in body)
          state.read_throughs = (body.read_throughs as { started_on: string | null; finished_on: string | null }[]).map((r, i) => ({ id: i + 1, ...r, derived: false }));
        state.prompt = state.finished && state.rating === null && !state.dismissed;
      } else if (method === "POST") state = { ...state, dismissed: true, prompt: false };
      return json(200, state);
    }),
  );
  return sent;
}

async function render(el: ReturnType<typeof createElement>) {
  const host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  await act(async () => root!.render(createElement(MemoryRouter, null, el)));
  await act(async () => new Promise((r) => setTimeout(r, 0)));
}

const click = async (el: Element | null | undefined) => {
  expect(el).toBeTruthy();
  await act(async () => (el as HTMLElement).click());
  await act(async () => new Promise((r) => setTimeout(r, 0)));
};
const button = (text: string) => [...document.querySelectorAll("button")].find((b) => b.textContent === text);
function type(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("the book detail page's rating and notes", () => {
  it("offers Rate it? for a finished book, rates in half stars, and can dismiss", async () => {
    const sent = server(FINISHED);
    await render(createElement(Section, { work: { id: 7, title: "Moby-Dick" } }));
    const section = document.querySelector('[aria-label="Your rating and notes for Moby-Dick"]')!;
    expect(section.textContent).toContain("You finished this book.");
    expect(section.textContent).toContain("from your reading progress");
    expect(section.querySelectorAll('[role="radio"]')).toHaveLength(10);
    // The left half of the fourth star is 3½.
    await click(section.querySelector('[aria-label="3½ stars"]'));
    expect(sent).toEqual([{ method: "PUT", url: "/api/works/7/review", body: { rating: 3.5 } }]);
    expect(section.textContent).not.toContain("You finished this book.");
    expect(section.querySelector('[aria-checked="true"]')?.getAttribute("aria-label")).toBe("3½ stars");
    expect([...section.querySelectorAll(".star")].map((s) => s.getAttribute("data-fill"))).toEqual(["1", "1", "1", "0.5", "0"]);
    // Choosing it again clears it.
    await click(section.querySelector('[aria-label="3½ stars"]'));
    expect(sent[1].body).toEqual({ rating: null });
    await click(button("Not now"));
    expect(sent[2]).toEqual({ method: "POST", url: "/api/works/7/review/dismiss", body: {} });
    expect(document.body.textContent).not.toContain("You finished this book.");
  });

  it("keeps derived dates derived when only the notes change", async () => {
    const sent = server(FINISHED);
    await render(createElement(Section, { work: { id: 7, title: "Moby-Dick" } }));
    await click(button("Add notes or dates"));
    await act(async () => type(document.querySelector("textarea")!, "# Verdict\n\n**Call me** <b>Ishmael</b>"));
    await click(button("Save"));
    expect(sent).toEqual([{ method: "PUT", url: "/api/works/7/review", body: { notes: "# Verdict\n\n**Call me** <b>Ishmael</b>" } }]);
    const notes = document.querySelector(".review-notes")!;
    expect(notes.querySelector("strong")?.textContent).toBe("Verdict");
    expect(notes.querySelector("b")).toBeNull(); // HTML in notes stays text
    expect(notes.textContent).toContain("<b>Ishmael</b>");
    expect(notes.querySelector("h1, h2, h3, h4")).toBeNull();
  });

  it("corrects dates and adds a re-read, refusing a finish before the start", async () => {
    const sent = server(FINISHED);
    await render(createElement(Section, { work: { id: 7, title: "Moby-Dick" } }));
    await click(button("Add notes or dates"));
    await click(button("Add a re-read"));
    const dates = () => [...document.querySelectorAll<HTMLInputElement>('input[type="date"]')];
    expect(dates().map((d) => d.value)).toEqual(["2026-01-05", "2026-02-01", "", ""]);
    await act(async () => type(dates()[2], "2026-03-10"));
    await act(async () => type(dates()[3], "2026-03-01"));
    await click(button("Save"));
    expect(sent).toEqual([]);
    expect(document.querySelector('[role="alert"]')?.textContent).toBe("A read cannot finish before it starts.");
    await act(async () => type(dates()[3], "2026-03-20"));
    await act(async () => type(dates()[0], "2025-12-30"));
    await click(button("Save"));
    expect(sent[0].body).toEqual({
      notes: "",
      read_throughs: [
        { started_on: "2025-12-30", finished_on: "2026-02-01" },
        { started_on: "2026-03-10", finished_on: "2026-03-20" },
      ],
    });
    const listed = [...document.querySelectorAll(".review-dates li")].map((li) => li.textContent);
    expect(listed).toHaveLength(2);
    expect(listed.join(" ")).not.toContain("from your reading progress");
  });

  it("shows the server's reason when a save is refused", async () => {
    server(FINISHED, (m) => (m === "PUT" ? json(422, { detail: "future_date", message: "Dates cannot be in the future." }) : null));
    await render(createElement(Section, { work: { id: 7, title: "Moby-Dick" } }));
    await click(document.querySelector('[aria-label="5 stars"]'));
    expect(store.toast).toHaveBeenCalledWith("Dates cannot be in the future.", "error");
  });
});

describe("the reader's Rate it? at the end of a book", () => {
  it("rates from the prompt and then goes away", async () => {
    const sent = server({ ...FINISHED, finished: false, prompt: false });
    await render(createElement(RatePrompt, { workId: 7 }));
    expect(document.body.textContent).toContain("Rate it?");
    await click(document.querySelector('[aria-label="4 stars"]'));
    expect(sent).toEqual([{ method: "PUT", url: "/api/works/7/review", body: { rating: 4 } }]);
    expect(document.body.textContent).not.toContain("Rate it?");
    expect(store.toast).toHaveBeenCalledWith("Rated 4 stars");
  });

  it("is not shown once rated or dismissed", async () => {
    server({ ...FINISHED, rating: 2 });
    await render(createElement(RatePrompt, { workId: 7 }));
    expect(document.body.textContent).toBe("");
    act(() => root?.unmount());
    server({ ...FINISHED, dismissed: true, prompt: false });
    await render(createElement(RatePrompt, { workId: 7 }));
    expect(document.body.textContent).toBe("");
  });
});

describe("review helpers", () => {
  it("labels and fills stars in halves", () => {
    expect([0.5, 1, 4.5, null].map(starsLabel)).toEqual(["½ stars", "1 star", "4½ stars", "No rating"]);
    expect([1, 2, 3].map((i) => starFill(2.5, i))).toEqual([1, 1, 0.5]);
  });

  it("checks read-throughs as the server does", () => {
    expect(readsBody([{ started_on: "", finished_on: "" }], "2026-10-28")).toMatch(/start or finish/);
    expect(readsBody([{ started_on: "", finished_on: "2026-10-29" }], "2026-10-28")).toMatch(/future/);
    expect(readsBody([{ started_on: "2026-10-28", finished_on: "" }], "2026-10-28")).toEqual([{ started_on: "2026-10-28", finished_on: null }]);
    expect(localToday(new Date(2026, 0, 2, 23, 59))).toBe("2026-01-02");
  });

  it("never turns notes into HTML", () => {
    const types = notesTokens("<script>x</script>\n\n# Title").map((t) => t.type);
    expect(types).not.toContain("html_block");
    expect(types).not.toContain("heading_open");
  });
});
