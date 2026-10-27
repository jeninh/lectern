import { describe, expect, it } from "vitest";
import { convertHint, editionHint, firstPreviewPage, formatPages, numberingSummary, parsePages, previewDoc, ruleSummary, type PdfAnalysis, type PdfEpubReport, type PdfRules } from "../pdf2epub";

const rules = (patch: Partial<PdfRules> = {}): PdfRules => ({
  version: 1,
  page_numbers: { keep: true, offset: 0 },
  chapters: [],
  verse: "auto",
  verse_pages: [],
  footnotes: "end",
  figures: "keep",
  tables: "auto",
  maths: "image",
  styles: true,
  page_images: [4],
  drop_pages: [],
  ...patch,
});

const analysis = (patch: Partial<PdfAnalysis> = {}): PdfAnalysis => ({
  summary: { pages: 12, flagged_pages: [4], figures: 2, tables: 2, complex_tables: 1, maths: 1, footnotes: 3, dropcaps: 1, styled_words: 40 },
  body_size: 10,
  page_numbers: {
    source: "page_labels",
    ranges: [
      { scheme: "roman", first: "i", last: "ii", pages: [1, 2] },
      { scheme: "arabic", first: "5", last: "14", pages: [3, 12] },
    ],
  },
  running_heads_removed: 10,
  columns_pages: 0,
  chapters: [
    { index: 0, title: "One", page_start: 1, page_end: 5, from_outline: true },
    { index: 1, title: "Two", page_start: 6, page_end: 9, from_outline: true },
    { index: 2, title: "Three", page_start: 10, page_end: 12, from_outline: true },
  ],
  headings: [],
  footnotes: [],
  zones: [],
  matter: [],
  pages: [],
  preview_pages: [1, 2, 4, 7],
  rules: rules(),
  defaults: rules(),
  ...patch,
});

describe("page lists", () => {
  it("parses ranges and single pages into a sorted list without repeats", () => {
    expect(parsePages("7, 1-3,2  9–10", 20)).toEqual([1, 2, 3, 7, 9, 10]);
    expect(parsePages("", 20)).toEqual([]);
  });

  it("refuses pages outside the book, reversed ranges, and junk", () => {
    expect(() => parsePages("0", 5)).toThrow(/1 to 5/);
    expect(() => parsePages("4-6", 5)).toThrow(/1 to 5/);
    expect(() => parsePages("3-2", 5)).toThrow();
    expect(() => parsePages("two", 5)).toThrow(/not a page/);
  });

  it("formats runs back into ranges, so a round trip is stable", () => {
    expect(formatPages([9, 1, 2, 3, 7, 3])).toBe("1-3, 7, 9");
    expect(formatPages([5])).toBe("5");
    expect(formatPages([])).toBe("");
    expect(formatPages(parsePages("12, 10-11, 1", 20))).toBe("1, 10-12");
  });
});

describe("the review sheet's plain-language rules", () => {
  it("describes the numbering scheme the PDF uses", () => {
    expect(numberingSummary(analysis())).toBe("i–ii, then 5–14 (the PDF's own page labels)");
    const physical = analysis({ page_numbers: { source: "physical", ranges: [{ scheme: "arabic", first: "1", last: "3", pages: [1, 3] }] } });
    expect(numberingSummary(physical)).toBe("1–3 (no page labels: physical pages)");
  });

  it("counts chapters after merges and says what happens to notes, figures, tables, and maths", () => {
    const lines = ruleSummary(analysis(), rules({ chapters: [{ index: 2, title: null, merge: true }, { index: 0, title: "Prologue", merge: false }] }));
    expect(lines).toContain("2 chapters from the PDF's bookmarks, 1 renamed, 1 merged");
    expect(lines).toContain("3 footnotes: at the end of each chapter");
    expect(lines).toContain("2 figures: kept as images with captions");
    expect(lines).toContain("2 tables: simple ones as tables, 1 complex as images");
    expect(lines).toContain("1 formula: as images, text as alt");
    expect(lines).toContain("Pages kept as images: 4");
  });

  it("follows edits: no page numbers, pop-up notes, tables as text, verse only where marked", () => {
    const lines = ruleSummary(analysis(), rules({ page_numbers: { keep: false, offset: 0 }, footnotes: "popup", tables: "text", verse: "off", verse_pages: [3, 4, 5], page_images: [], drop_pages: [1, 2] }));
    expect(lines[0]).toBe("Page numbers left out");
    expect(lines).toContain("3 footnotes: after their paragraph (pop-ups)");
    expect(lines).toContain("2 tables: as plain text");
    expect(lines).toContain("Verse only on pages 3-5");
    expect(lines).toContain("Pages left out: 1-2");
    expect(lines.some((l) => l.startsWith("Pages kept as images"))).toBe(false);
  });

  it("leaves out what the PDF does not have", () => {
    const plain = analysis({ summary: { pages: 3, flagged_pages: [], figures: 0, tables: 0, complex_tables: 0, maths: 0, footnotes: 0, dropcaps: 0, styled_words: 0 } });
    const lines = ruleSummary(plain, rules({ page_images: [] }));
    expect(lines.some((l) => /footnote|figure|table|formula/.test(l))).toBe(false);
  });
});

describe("the side-by-side preview", () => {
  it("opens on the first low-confidence sample page", () => {
    expect(firstPreviewPage(analysis())).toBe(4);
    expect(firstPreviewPage(analysis({ summary: { ...analysis().summary, flagged_pages: [] } }))).toBe(1);
    expect(firstPreviewPage(analysis({ preview_pages: [] }))).toBeNull();
  });

  it("wraps the converted text in a document without scripts, and says when a page has none", () => {
    const doc = previewDoc("<p>Hello</p>");
    expect(doc).toContain("<p>Hello</p>");
    expect(doc).not.toMatch(/<script/i);
    expect(previewDoc("")).toContain("Nothing from this page is in the edition.");
  });
});

const report = (over: Partial<PdfEpubReport> = {}): PdfEpubReport => ({
  pdf_sha256: "0".repeat(64), status: "ready", pages: 12, sections: 3, page_list_entries: 12,
  labels_source: "physical", warnings: [], pages_without_text: [], unlinked_pages: [], ...over,
});

describe("editionHint", () => {
  it("says an edition of a scan was generated from OCR", () => {
    expect(editionHint({ report: report({ source: "ocr", ocr: { engine: "tesseract 5.3.0", language: "eng" } }), outdated: false }))
      .toBe("EPUB generated from OCR text of the scan · 3 sections, page numbers 1…12");
  });
  it("keeps the text PDF wording, including reports from before OCR existed", () => {
    expect(editionHint({ report: report({ sections: 1, labels_source: "page_labels" }), outdated: false }))
      .toBe("EPUB converted from the PDF · 1 section, page numbers from the PDF's page labels");
    expect(editionHint({ report: report({ source: "text_layer", page_list_entries: 0 }), outdated: false }))
      .toBe("EPUB converted from the PDF · 3 sections");
  });
  it("counts the pages kept as images", () => {
    expect(editionHint({ report: report({ page_images: [4, 9] }), outdated: false })).toBe("EPUB converted from the PDF · 3 sections, page numbers 1…12, 2 pages as images");
  });
  it("flags an edition built from an older recognition", () => {
    expect(editionHint({ report: report({ source: "ocr" }), outdated: true })).toMatch(/· out of date: the PDF's text changed since$/);
  });
});

describe("convertHint", () => {
  it("warns before converting a scan that its text comes from OCR", () => {
    expect(convertHint("ocr")).toContain("OCR of the scan (expect recognition errors)");
    expect(convertHint("text_layer")).not.toContain("OCR");
  });
});
