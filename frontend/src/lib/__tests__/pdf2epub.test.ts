import { describe, expect, it } from "vitest";
import { convertHint, editionHint, type PdfEpubReport } from "../pdf2epub";

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
