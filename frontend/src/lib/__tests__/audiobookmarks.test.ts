import { describe, expect, it } from "vitest";
import { type AudioBookmark, clipBounds, lastSeconds, markLabel, markReadAlongPath, markReaderPath, marksBySentence, MAX_CLIP_S, startAt } from "../audiobookmarks";
import type { BookFile } from "../types";

const EPUB_FILE = { id: 1, media_type: "application/epub+zip" } as BookFile;
const PDF_FILE = { id: 2, media_type: "application/pdf" } as BookFile;

function mark(over: Partial<AudioBookmark>): AudioBookmark {
  return {
    id: 1, work_id: 5, kind: "bookmark", start_s: 754, end_s: null, note: null, chapter: null, device: "Phone",
    created_at: "2026-10-28T09:30:00Z", updated_at: "2026-10-28T09:30:00Z",
    sentence_n: null, sentence_end_n: null, location: null, text: null,
    ...over,
  };
}

describe("lastSeconds (the Clip last 30 s shortcut)", () => {
  it("covers what was just heard, ending now", () => {
    expect(lastSeconds(754.2)).toEqual({ start: 724.2, end: 754.2 });
    expect(lastSeconds(100, 10)).toEqual({ start: 90, end: 100 });
  });
  it("never starts before the book, and is never empty at the very start", () => {
    expect(lastSeconds(12)).toEqual({ start: 0, end: 12 });
    expect(lastSeconds(0)).toEqual({ start: 0, end: 0.5 });
  });
});

describe("clipBounds (the clip handles)", () => {
  it("lets the dragged handle win: the other one gives way", () => {
    // Start dragged past the end: the end follows it.
    expect(clipBounds(60, 50, 1000, "start")).toEqual({ start: 60, end: 60.5 });
    // End dragged before the start: the start follows it.
    expect(clipBounds(60, 50, 1000, "end")).toEqual({ start: 49.5, end: 50 });
  });
  it("keeps a clip inside the book and no longer than the server allows", () => {
    expect(clipBounds(-5, 2000, 1000, "end")).toEqual({ start: 0, end: 1000 });
    expect(clipBounds(999.9, 1000, 1000, "start")).toEqual({ start: 999.5, end: 1000 });
    expect(clipBounds(0, 3000, 5000, "end")).toEqual({ start: 3000 - MAX_CLIP_S, end: 3000 });
    expect(clipBounds(100, 5000, 5000, "start")).toEqual({ start: 100, end: 100 + MAX_CLIP_S });
  });
});

describe("markLabel", () => {
  it("names a bookmark by its time and a clip by its span, with hours when there are any", () => {
    expect(markLabel(mark({}))).toBe("Bookmark 12:34");
    expect(markLabel(mark({ start_s: 3725, end_s: 3755.9 }))).toBe("Clip 1:02:05–1:02:35");
  });
});

describe("where a mark leads", () => {
  const aligned = mark({ sentence_n: 61, sentence_end_n: 62, location: { spine_index: 2, char: 285, length: 120 } });
  it("opens the EPUB reader at an aligned mark's passage, and the read-along at its time", () => {
    expect(markReaderPath({ id: 5, files: [EPUB_FILE] }, aligned)).toBe("/app/reader/5?at=2:285&len=120");
    expect(markReadAlongPath(aligned)).toBe("/app/read/5?t=754");
  });
  it("has no passage without alignment or without an EPUB to open", () => {
    expect(markReaderPath({ id: 5, files: [EPUB_FILE] }, mark({}))).toBeNull();
    expect(markReadAlongPath(mark({}))).toBeNull();
    expect(markReaderPath({ id: 5, files: [PDF_FILE] }, aligned)).toBeNull();
  });
  it("reads ?t= back, ignoring junk and negative times", () => {
    expect(startAt(new URLSearchParams("t=754.5"))).toBe(754.5);
    expect(startAt(new URLSearchParams("t=0"))).toBe(0);
    expect(startAt(new URLSearchParams("t=abc"))).toBeNull();
    expect(startAt(new URLSearchParams("t=-3"))).toBeNull();
    expect(startAt(new URLSearchParams(""))).toBeNull();
  });
});

describe("marksBySentence (the read-along marks)", () => {
  it("puts a clip on every sentence it covers and a bookmark on one, skipping unaligned marks", () => {
    const clip = mark({ id: 2, kind: "clip", end_s: 800, sentence_n: 3, sentence_end_n: 5 });
    const one = mark({ id: 3, sentence_n: 5, sentence_end_n: 5 });
    const by = marksBySentence([clip, one, mark({ id: 4 })]);
    expect([...by.keys()].sort()).toEqual([3, 4, 5]);
    expect(by.get(5)!.map((m) => m.id)).toEqual([2, 3]);
    expect(by.get(3)!.map((m) => m.id)).toEqual([2]);
  });
});
