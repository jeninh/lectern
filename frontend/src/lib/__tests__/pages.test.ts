import { describe, expect, it } from "vitest";
import { bookFiles, positionLabel, readerPath } from "../formats";
import { fitScale, spreadOf, stepZoom, turn } from "../pages";
import { ACCEPT, mediaTypeOf } from "../uploads";
import type { BookFile } from "../types";

const file = (id: number, media_type: string): BookFile =>
  ({ id, kind: "ebook", media_type, rel_path: "x", size: 1, sha256: "s", partial_md5: null, download_url: "", added_at: null }) as BookFile;

describe("two-page spreads", () => {
  it("shows the cover alone, then pairs 2–3, 4–5, and a lone last page", () => {
    expect(spreadOf(1, 6, true)).toEqual([1]);
    expect(spreadOf(2, 6, true)).toEqual([2, 3]);
    expect(spreadOf(3, 6, true)).toEqual([2, 3]); // any page of a pair shows the pair
    expect(spreadOf(6, 6, true)).toEqual([6]);
    expect(spreadOf(5, 5, true)).toEqual([4, 5]);
    expect(spreadOf(4, 6, false)).toEqual([4]);
  });

  it("turns a whole spread at a time and stops at the ends", () => {
    expect(turn(1, 6, true, 1)).toBe(2);
    expect(turn(3, 6, true, 1)).toBe(4);
    expect(turn(4, 6, true, -1)).toBe(2);
    expect(turn(2, 6, true, -1)).toBe(1);
    expect(turn(6, 6, true, 1)).toBe(6);
    expect(turn(1, 6, true, -1)).toBe(1);
    expect(turn(3, 6, false, 1)).toBe(4);
  });
});

describe("fit and zoom", () => {
  it("fit width fills the width; fit page also fits the height", () => {
    // A tall page (100×300) on a wide stage (400×300).
    expect(fitScale("width", 100, 300, 400, 300)).toBe(4);
    expect(fitScale("page", 100, 300, 400, 300)).toBe(1);
    expect(fitScale("page", 100, 300, 400, 300, 2)).toBe(2);
  });
  it("steps through the zoom levels and clamps", () => {
    expect(stepZoom(1, 1)).toBe(1.25);
    expect(stepZoom(1, -1)).toBe(0.8);
    expect(stepZoom(1.1, 1)).toBe(1.25);
    expect(stepZoom(4, 1)).toBe(4);
    expect(stepZoom(0.5, -1)).toBe(0.5);
  });
});

describe("formats", () => {
  it("uploads accept PDF and CBZ but not CBR", () => {
    expect(mediaTypeOf("Atlas.PDF")).toBe("application/pdf");
    expect(mediaTypeOf("saga.cbz")).toBe("application/vnd.comicbook+zip");
    expect(mediaTypeOf("saga.cbr")).toBeNull();
    expect(ACCEPT).toContain(".cbz");
    expect(ACCEPT).not.toContain(".cbr");
  });
  it("Read opens the EPUB reader when there is an EPUB, else the page viewer", () => {
    const both = { id: 7, files: [file(3, "application/pdf"), file(2, "application/epub+zip")] };
    expect(bookFiles(both).map((f) => f.id)).toEqual([2, 3]);
    expect(readerPath(both)).toBe("/app/reader/7");
    expect(readerPath(both, both.files[0])).toBe("/app/reader/7?file=3");
    expect(readerPath({ id: 8, files: [file(5, "application/vnd.comicbook+zip")] })).toBe("/app/reader/8?file=5");
  });
  it("KOReader page numbers read as pages", () => {
    expect(positionLabel("12")).toBe("Page 12");
    expect(positionLabel("/body/DocFragment[4]/body/p[1]/text().0")).toBe("Section 4");
  });
});
