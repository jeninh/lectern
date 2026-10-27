import { describe, expect, it } from "vitest";
import { annotationPath, annotationWhere, byChapter } from "../annotations";
import type { Annotation, BookFile } from "../types";

const EPUB_FILE = { id: 1, media_type: "application/epub+zip" } as BookFile;
const PDF_FILE = { id: 2, media_type: "application/pdf" } as BookFile;

function ann(over: Partial<Annotation>): Annotation {
  return {
    id: 1, key: "k", text: "Three times Della counted it", note: null, chapter: null, color: null,
    page: null, pageno: null, created: "2026-10-01 09:30:00", updated: "2026-10-01 09:30:00",
    device: "Kindle", file: { id: 1, media_type: "application/epub+zip" }, location: null, sentence_n: null,
    ...over,
  };
}

describe("annotationWhere", () => {
  it("shows the PDF page before KOReader's page number, and minutes not seconds", () => {
    expect(annotationWhere(ann({ page: 4, pageno: 9 }))).toBe("Page 4 · 2026-10-01 09:30");
    expect(annotationWhere(ann({ pageno: 9 }))).toBe("Page 9 · 2026-10-01 09:30");
    expect(annotationWhere(ann({}))).toBe("2026-10-01 09:30");
  });
});

describe("byChapter", () => {
  it("starts a group only where the chapter changes, keeping chapterless ones in the current group", () => {
    const list = [ann({ id: 1, chapter: "I" }), ann({ id: 2, chapter: null }), ann({ id: 3, chapter: "I" }), ann({ id: 4, chapter: "II" }), ann({ id: 5, chapter: "I" })];
    expect(byChapter(list).map((g) => [g.chapter, g.items.map((a) => a.id)])).toEqual([
      ["I", [1, 2, 3]],
      ["II", [4]],
      ["I", [5]],
    ]);
    expect(byChapter([ann({ id: 7 })]).map((g) => g.chapter)).toEqual([null]);
  });
});

describe("annotationPath", () => {
  const both = { id: 5, files: [EPUB_FILE, PDF_FILE] };
  it("opens the EPUB reader at the placed passage, even for a highlight made in the PDF", () => {
    const a = ann({ location: { spine_index: 2, char: 285, length: 28 }, page: 4, file: { id: 2, media_type: "application/pdf" } });
    expect(annotationPath(both, a)).toBe("/app/reader/5?at=2:285&len=28");
  });
  it("opens a PDF highlight that could not be placed at its page, finding its first words", () => {
    const a = ann({ page: 4, file: { id: 2, media_type: "application/pdf" }, text: "Della  finished\nher cry and attended" });
    expect(annotationPath(both, a)).toBe("/app/reader/5?file=2&page=4&find=Della%20finished%20her%20cry%20and%20attended");
  });
  it("has nowhere to go without an EPUB placement or a PDF page of this work", () => {
    expect(annotationPath({ id: 5, files: [PDF_FILE] }, ann({ location: { spine_index: 0, char: 0, length: 1 } }))).toBeNull();
    expect(annotationPath(both, ann({}))).toBeNull();
    expect(annotationPath({ id: 5, files: [EPUB_FILE] }, ann({ page: 3, file: { id: 9, media_type: "application/pdf" } }))).toBeNull();
  });
});
