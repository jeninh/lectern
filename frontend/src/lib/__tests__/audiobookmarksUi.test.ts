// @vitest-environment jsdom
// Audiobook bookmarks and clips in the UI (components/AudioBookmarks.tsx, issue #81): the
// one-tap bookmark, the "last 30 s" clip, and the list that plays them back and leads to the
// passage.
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { AudioBookmark } from "../audiobookmarks";
import type { Work } from "../types";

const WORK = { id: 5, title: "The Gift of the Magi", author: "O. Henry", files: [{ id: 1, media_type: "application/epub+zip" }] } as unknown as Work;
const OTHER = { id: 8, title: "Moby-Dick", author: "Herman Melville", files: [] } as unknown as Work;
const player = vi.hoisted(() => ({
  work: null as unknown,
  t: 754.237,
  duration: 4000,
  chapter: { title: "Chapter 3", start: 700, end: 900 },
  playing: false,
  clip: null,
  open: vi.fn(async () => {}),
  playClip: vi.fn(async () => {}),
  seek: vi.fn(),
  play: vi.fn(),
  pause: vi.fn(),
}));
const store = vi.hoisted(() => ({ toast: vi.fn(), deviceName: "Phone" }));
vi.mock("../player", () => ({
  usePlayer: () => player,
  clock: (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`,
}));
vi.mock("../store", () => ({ useStore: () => store }));

const { BookmarkButton, ClipEditor, MarkList } = await import("../../components/AudioBookmarks");

beforeAll(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

let root: ReturnType<typeof createRoot> | null = null;
afterEach(() => {
  act(() => root?.unmount());
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  player.work = null;
});

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function mark(over: Partial<AudioBookmark>): AudioBookmark {
  return {
    id: 1, work_id: 5, kind: "bookmark", start_s: 754, end_s: null, note: null, chapter: null, device: "Phone",
    created_at: "2026-10-28T09:30:00Z", updated_at: "2026-10-28T09:30:00Z",
    sentence_n: null, sentence_end_n: null, location: null, text: null,
    ...over,
  };
}

async function render(el: ReturnType<typeof createElement>) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (u: string, init?: RequestInit) => {
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      calls.push({ url: u, method: init?.method ?? "GET", body });
      if (init?.method === "POST") return json(201, mark({ id: 77, ...body }));
      return json(200, { deleted: true });
    }),
  );
  const host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  await act(async () => root!.render(createElement(MemoryRouter, null, el)));
  return calls;
}

function button(name: RegExp): HTMLButtonElement {
  const b = [...document.querySelectorAll<HTMLButtonElement>("button")].find((x) => name.test(x.getAttribute("aria-label") ?? x.textContent ?? ""));
  if (!b) throw new Error(`no button ${name}`);
  return b;
}

describe("the bookmark button", () => {
  it("bookmarks the moment being heard, in its chapter, from this device, in one tap", async () => {
    player.work = WORK;
    const calls = await render(createElement(BookmarkButton));
    await act(async () => button(/Bookmark this moment/).click());
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.url).toBe("/api/works/5/audio-bookmarks");
    expect(post.body).toEqual({ start_s: 754.24, chapter: "Chapter 3", device: "Phone" });
    expect(store.toast).toHaveBeenCalledWith("Bookmarked 12:34.", "info", undefined);
  });
  it("is not there with nothing loaded in the player", async () => {
    await render(createElement(BookmarkButton));
    expect(document.querySelector("button")).toBeNull();
  });
});

describe("the clip editor", () => {
  it("starts as the last 30 seconds and saves those times with the note", async () => {
    player.work = WORK;
    const calls = await render(createElement(ClipEditor, { onDone: () => {} }));
    expect(document.querySelector(".clip-editor h3")?.textContent).toContain("12:04–12:34 · 30 s");
    const note = document.querySelector<HTMLTextAreaElement>("textarea")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(note, " the turn ");
      note.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => button(/Play clip/).click());
    expect(player.playClip).toHaveBeenCalledWith(WORK, 724.237, 754.237);
    await act(async () => button(/Save clip/).click());
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.body).toMatchObject({ start_s: 724.237, end_s: 754.237, note: "the turn", chapter: "Chapter 3" });
  });
});

describe("the list of marks", () => {
  it("plays a clip from the original tracks, and a bookmark of another book from its time", async () => {
    player.work = OTHER;
    const clip = mark({ id: 2, kind: "clip", start_s: 60, end_s: 90 });
    await render(createElement(MarkList, { work: WORK, marks: [clip, mark({ id: 3, start_s: 754 })] }));
    await act(async () => button(/Play Clip 1:00–1:30/).click());
    expect(player.playClip).toHaveBeenCalledWith(WORK, 60, 90);
    await act(async () => button(/Play Bookmark 12:34/).click());
    expect(player.open).toHaveBeenCalledWith(WORK, { at: 754, autoplay: true });
    expect(player.seek).not.toHaveBeenCalled();
  });
  it("seeks the book already playing instead of reopening it", async () => {
    player.work = WORK;
    await render(createElement(MarkList, { work: WORK, marks: [mark({ start_s: 754 })] }));
    await act(async () => button(/Play Bookmark/).click());
    expect(player.seek).toHaveBeenCalledWith(754);
    expect(player.play).toHaveBeenCalled();
    expect(player.open).not.toHaveBeenCalled();
  });
  it("marks each one as audio, shows the passage, and leads to it in the reader and the read-along", async () => {
    const aligned = mark({ sentence_n: 61, sentence_end_n: 61, location: { spine_index: 2, char: 285, length: 40 }, text: "Della finished her cry", note: "lovely" });
    await render(createElement(MarkList, { work: WORK, marks: [aligned, mark({ id: 4, start_s: 9 })] }));
    const items = document.querySelectorAll("li.audio-mark");
    expect(items[0].querySelector(".audio-mark-badge")?.textContent).toContain("Audio");
    expect(items[0].querySelector("blockquote")?.textContent).toBe("Della finished her cry");
    expect(items[0].querySelector(".highlight-note")?.textContent).toBe("Note: lovely");
    const links = [...items[0].querySelectorAll("a")].map((a) => [a.textContent, a.getAttribute("href")]);
    expect(links).toEqual([["Open in reader", "/app/reader/5?at=2:285&len=40"], ["Read along", "/app/read/5?t=754"]]);
    // Unaligned: time and chapter only, nowhere in the text to go.
    expect(items[1].querySelectorAll("a")).toHaveLength(0);
  });
  it("deletes one and edits its note", async () => {
    const calls = await render(createElement(MarkList, { work: WORK, marks: [mark({ id: 6 })] }));
    await act(async () => button(/Add note/).click());
    const note = document.querySelector<HTMLTextAreaElement>("textarea")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(note, "  later  ");
      note.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => button(/Save note/).click());
    expect(calls.find((c) => c.method === "PATCH")).toMatchObject({ url: "/api/audio-bookmarks/6", body: { note: "later" } });
    await act(async () => button(/Delete Bookmark/).click());
    expect(calls.find((c) => c.method === "DELETE")?.url).toBe("/api/audio-bookmarks/6");
  });
});
