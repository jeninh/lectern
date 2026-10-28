// PDF → EPUB conversion (issues #28, #63): turn a work's text PDF into a reflowable EPUB
// edition, with the PDF's own page numbers kept as metadata (a page-list nav the Kindle and
// KOReader can show). The original PDF is kept; the EPUB is a separate edition. A scanned PDF
// converts once OCR has given it text (issue #62), and its edition is labelled as generated
// from OCR.
//
// The flow is analyse → review rules → convert: an analysis job finds the structure (figures,
// tables, footnotes, maths, page confidence), the review sheet shows the detected rules with a
// side-by-side preview (PDF page beside its converted text), and the conversion runs with the
// rules the person confirmed. The rules are saved with the edition for a re-run.

import { del, get, post } from "./api";

export interface PdfEpubReport {
  pdf_sha256: string | null;
  status: string;
  /** Where the converted text came from (older reports have neither field). */
  source?: "text_layer" | "ocr";
  ocr?: { engine: string; language: string } | null;
  pages: number;
  sections: number;
  page_list_entries: number;
  labels_source: "page_labels" | "physical";
  warnings: string[];
  pages_without_text: number[];
  unlinked_pages: number[];
  flagged_pages?: number[];
  page_images?: number[];
  converted?: Record<string, number>;
  rules?: PdfRules;
}

export interface PdfEpubJob {
  id: number;
  status: "queued" | "leased" | "succeeded" | "failed" | "cancelled";
  error: string | null;
  mode?: "analyse" | "convert";
  sections: number | null;
  /** On GET …/pdf-epub: false when only the owner or whoever started it may cancel. */
  can_cancel?: boolean;
}

export interface PdfEpubState {
  work_id: number;
  eligible: boolean;
  reason: string | null;
  job: PdfEpubJob | null;
  edition: { file: string; size: number; sha256: string; added_at: string } | null;
  report: PdfEpubReport | null;
  analysed?: boolean;
  /** The work's text PDF: its own text layer, or OCR of a scan. */
  source: "text_layer" | "ocr" | null;
  /** The edition was built from an older text (the scan was recognised again). */
  outdated: boolean;
}

export interface PdfRules {
  version: 1;
  page_numbers: { keep: boolean; offset: number };
  chapters: { index: number; title: string | null; merge: boolean }[];
  verse: "auto" | "off";
  verse_pages: number[];
  footnotes: "end" | "popup" | "inline";
  figures: "keep" | "drop";
  tables: "auto" | "image" | "text";
  maths: "image" | "text";
  styles: boolean;
  page_images: number[];
  drop_pages: number[];
}

export interface PageConfidence {
  page: number;
  confidence: number;
  flagged: boolean;
  reasons: string[];
  figures: number;
  tables: number;
  maths: number;
  content: boolean;
}

export interface PdfAnalysis {
  summary: {
    pages: number;
    flagged_pages: number[];
    figures: number;
    tables: number;
    complex_tables: number;
    maths: number;
    footnotes: number;
    dropcaps: number;
    styled_words: number;
  };
  body_size: number;
  page_numbers: { source: "page_labels" | "physical"; ranges: { scheme: string; first: string; last: string; pages: [number, number] }[] };
  running_heads_removed: number;
  columns_pages: number;
  chapters: { index: number; title: string; page_start: number; page_end: number; from_outline: boolean }[];
  headings: { size: number; level: number; count: number; example: string }[];
  footnotes: { page: number; marker: string; text: string }[];
  zones: { id: string; page: number; kind: "figure" | "table" | "math"; complex: boolean; photo: boolean }[];
  matter: { page: number; kind: "cover" | "copyright" | "index" }[];
  pages: PageConfidence[];
  preview_pages: number[];
  rules: PdfRules;
  defaults: PdfRules;
}

export interface PreviewPage {
  page: number;
  label: string;
  confidence: number | null;
  reasons: string[];
  as_image: boolean;
  dropped: boolean;
  html: string;
  image: boolean;
}

/** What the "EPUB from PDF" row says about the converted edition. */
export function editionHint(st: Pick<PdfEpubState, "report" | "outdated">): string {
  const r = st.report;
  const sections = r?.sections ?? 0;
  let hint = `${r?.source === "ocr" ? "EPUB generated from OCR text of the scan" : "EPUB converted from the PDF"} · ${sections} section${
    sections === 1 ? "" : "s"
  }`;
  if (r?.page_list_entries) {
    hint += `, page numbers ${r.labels_source === "page_labels" ? "from the PDF's page labels" : "1…" + r.pages}`;
  }
  const images = r?.page_images?.length ?? 0;
  if (images) hint += `, ${images} page${images === 1 ? "" : "s"} as images`;
  if (st.outdated) hint += " · out of date: the PDF's text changed since";
  return hint;
}

/** The hint before converting: a scan's text comes from OCR, so the edition will too. */
export const convertHint = (source: PdfEpubState["source"]) =>
  source === "ocr"
    ? "Reflowable text from the OCR of the scan (expect recognition errors), figures and tables, the PDF's own page numbers kept; the PDF stays"
    : "Reflowable text, figures, tables, and footnotes, the PDF's own page numbers kept; the PDF stays";

export const pdfEpubState = (workId: number) => get<PdfEpubState>(`/api/works/${workId}/pdf-epub`);
export const convertPdf = (workId: number, body?: { rules?: PdfRules; reconvert?: boolean }) =>
  post<{ status: string; job_id: number }>(`/api/works/${workId}/pdf-epub`, body ?? {});
export const analysePdf = (workId: number) =>
  post<{ status: string; job_id: number }>(`/api/works/${workId}/pdf-epub/analyse`);
export const pdfAnalysis = (workId: number) => get<PdfAnalysis>(`/api/works/${workId}/pdf-epub/analysis`);
export const previewPdfEpub = (workId: number, pages: number[], rules: PdfRules) =>
  post<{ pages: PreviewPage[] }>(`/api/works/${workId}/pdf-epub/preview`, { pages, rules });
export const previewImageUrl = (workId: number, page: number) => `/api/works/${workId}/pdf-epub/preview/${page}.jpg`;
export const retryPdf = (workId: number) =>
  post<{ status: string; job_id: number }>(`/api/works/${workId}/pdf-epub/retry`);
export const removePdfEpub = (workId: number) => del<{ removed: boolean }>(`/api/works/${workId}/pdf-epub`);
export const cancelPdfJob = (jobId: number) => post<unknown>(`/api/jobs/${jobId}/cancel`);

// --- pure helpers (tested in __tests__/pdf2epub.test.ts) ------------------------------------

/** "1-3, 7" → [1, 2, 3, 7]: sorted, unique, within 1…max. Throws on anything else. */
export function parsePages(text: string, max: number): number[] {
  const out = new Set<number>();
  for (const part of text.split(/[,\s]+/).filter(Boolean)) {
    const m = /^(\d+)(?:[-–](\d+))?$/.exec(part);
    if (!m) throw new Error(`"${part}" is not a page or a range like 3-7`);
    const a = Number(m[1]);
    const b = m[2] ? Number(m[2]) : a;
    if (a < 1 || b < a || b > max) throw new Error(`${part}: pages run from 1 to ${max}`);
    if (b - a > 10000) throw new Error(`${part}: too many pages`);
    for (let p = a; p <= b; p++) out.add(p);
  }
  return [...out].sort((x, y) => x - y);
}

/** [1, 2, 3, 7] → "1-3, 7". */
export function formatPages(pages: number[]): string {
  const sorted = [...new Set(pages)].sort((x, y) => x - y);
  const parts: string[] = [];
  for (let i = 0; i < sorted.length; ) {
    let j = i;
    while (j + 1 < sorted.length && sorted[j + 1] === sorted[j] + 1) j++;
    parts.push(j > i ? `${sorted[i]}-${sorted[j]}` : `${sorted[i]}`);
    i = j + 1;
  }
  return parts.join(", ");
}

/** The detected numbering in words: "i–ii, then 5–340". */
export function numberingSummary(a: PdfAnalysis): string {
  const runs = a.page_numbers.ranges.map((r) => (r.first === r.last ? r.first : `${r.first}–${r.last}`));
  if (!runs.length) return "No pages";
  const text = runs.length > 4 ? `${runs.slice(0, 4).join(", then ")}, …` : runs.join(", then ");
  return a.page_numbers.source === "page_labels" ? `${text} (the PDF's own page labels)` : `${text} (no page labels: physical pages)`;
}

/** Each rule in plain language, as the review sheet's summary shows it. */
export function ruleSummary(a: PdfAnalysis, r: PdfRules): string[] {
  const s = a.summary;
  const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
  const lines: string[] = [];
  lines.push(r.page_numbers.keep ? `Page numbers kept: ${numberingSummary(a)}${r.page_numbers.offset ? `, shifted by ${r.page_numbers.offset}` : ""}` : "Page numbers left out");
  const merged = r.chapters.filter((c) => c.merge).length;
  const renamed = r.chapters.filter((c) => c.title).length;
  lines.push(`${plural(a.chapters.length - merged, "chapter")} ${a.chapters.some((c) => c.from_outline) ? "from the PDF's bookmarks" : "from headings"}${renamed ? `, ${renamed} renamed` : ""}${merged ? `, ${merged} merged` : ""}`);
  if (s.footnotes) lines.push(`${plural(s.footnotes, "footnote")}: ${{ end: "at the end of each chapter", popup: "after their paragraph (pop-ups)", inline: "left as text" }[r.footnotes]}`);
  if (s.figures) lines.push(`${plural(s.figures, "figure")}: ${r.figures === "keep" ? "kept as images with captions" : "left out (captions kept)"}`);
  if (s.tables)
    lines.push(`${plural(s.tables, "table")}: ${{ auto: s.complex_tables ? `simple ones as tables, ${s.complex_tables} complex as images` : "as HTML tables", image: "as images", text: "as plain text" }[r.tables]}`);
  if (s.maths) lines.push(`${plural(s.maths, "formula", "formulas")}: ${r.maths === "image" ? "as images, text as alt" : "as text"}`);
  lines.push(r.verse === "auto" ? `Verse detected automatically${r.verse_pages.length ? `, and on pages ${formatPages(r.verse_pages)}` : ""}` : r.verse_pages.length ? `Verse only on pages ${formatPages(r.verse_pages)}` : "No verse");
  lines.push(r.styles ? "Italic, bold, small caps, and superscripts kept" : "Plain text (no inline styles)");
  if (r.page_images.length) lines.push(`Pages kept as images: ${formatPages(r.page_images)}`);
  if (r.drop_pages.length) lines.push(`Pages left out: ${formatPages(r.drop_pages)}`);
  return lines;
}

/** The page shown first in the preview: the first flagged page, else the first sample. */
export function firstPreviewPage(a: PdfAnalysis): number | null {
  return a.summary.flagged_pages.find((p) => a.preview_pages.includes(p)) ?? a.preview_pages[0] ?? null;
}

/** A self-contained, script-free document for the converted side of the preview iframe. */
export function previewDoc(html: string): string {
  return (
    "<!doctype html><html><head><meta charset=\"utf-8\"><style>" +
    "body{font:16px/1.5 Georgia,serif;margin:12px;color:#222;background:#fff}" +
    ".sc{font-variant:small-caps}p.verse{margin-left:2em}blockquote{margin:1em 2em}" +
    "table{border-collapse:collapse}th,td{border:1px solid #888;padding:2px 6px}" +
    "aside{font-size:.9em;border-top:1px solid #ccc;margin-top:.5em}figcaption,.caption{font-style:italic;font-size:.9em}" +
    ".ph{display:block;border:1px dashed #999;padding:8px;color:#555;background:#f6f6f6;font:13px/1.4 sans-serif}" +
    "</style></head><body>" +
    (html || "<p style=\"color:#777;font:13px sans-serif\">Nothing from this page is in the edition.</p>") +
    "</body></html>"
  );
}

// --- many books at once: the Library's selection bar (issue #61) --------------------------------

/** One selected book's answer: queued (or already queued) with its job, or skipped/failed and why. */
export interface BulkConvertResult {
  work_id: number;
  title?: string;
  ok: boolean;
  status: "queued" | "already_queued" | "skipped" | "failed";
  job_id?: number;
  error?: string;
}
export type BulkConvertJob = PdfEpubJob & { work_id: number };

export const convertMany = (target: object) => post<{ results: BulkConvertResult[] }>("/api/works/pdf-epub", target);
export const bulkConvertJobs = (ids: number[]) => get<{ jobs: BulkConvertJob[] }>(`/api/pdf-epub/jobs?ids=${ids.join(",")}`);

/** Why a book was skipped or failed, in plain words (the server's codes). */
export const CONVERT_REASONS: Record<string, string> = {
  already_epub: "already an EPUB",
  converted: "already an EPUB",
  unsupported: "no PDF to convert",
  no_text_pdf: "no text in the PDF yet (a scan needs OCR first)",
  analysing: "being analysed for review; convert it from its details",
  not_found: "no longer exists",
  internal_error: "an unexpected server error",
  pdf_changed: "the PDF changed while it was queued",
  text_changed: "the PDF's text changed while it was queued",
  work_gone: "the book was trashed",
};
const reason = (code?: string | null) => (code ? CONVERT_REASONS[code] ?? code : "unknown error");
const books = (n: number) => `${n} ${n === 1 ? "book" : "books"}`;

/** One toast line for a bulk conversion, in the style of `bulkSummary`: how many were queued,
 * then the skipped and failed counts with each reason once. */
export function convertSummary(results: BulkConvertResult[]): { text: string; queued: number; skipped: number; failed: number } {
  const queued = results.filter((r) => r.ok).length;
  const of = (st: BulkConvertResult["status"]) => results.filter((r) => r.status === st);
  const why = (rs: BulkConvertResult[]) => [...new Set(rs.map((r) => reason(r.error)))].join("; ");
  const skipped = of("skipped");
  const failed = of("failed");
  const parts = [`Queued EPUB conversion for ${books(queued)}`];
  if (skipped.length) parts.push(`skipped ${books(skipped.length)} (${why(skipped)})`);
  if (failed.length) parts.push(`${books(failed.length)} failed (${why(failed)})`);
  return { text: `${parts.join("; ")}.`, queued, skipped: skipped.length, failed: failed.length };
}

export type ConvertRowState = "waiting" | "converting" | "done" | "failed" | "cancelled" | "skipped";
export interface ConvertRow {
  work_id: number;
  title: string;
  state: ConvertRowState;
  detail: string | null;
}

/** Per-book progress: each selected book with where its conversion is, from the bulk answer
 * and the latest job states (`jobs`; null before the first answer, when every job is still
 * "waiting"; after it, a job left out is gone, e.g. its book was trashed). `active`: some
 * conversion is still queued or running, so keep polling. */
export function convertProgress(results: BulkConvertResult[], jobs: BulkConvertJob[] | null): { rows: ConvertRow[]; active: boolean; done: number; failed: number } {
  const byId = new Map((jobs ?? []).map((j) => [j.id, j]));
  const rows = results.map((r): ConvertRow => {
    const title = r.title ?? `Book ${r.work_id}`;
    if (!r.ok) return { work_id: r.work_id, title, state: r.status === "skipped" ? "skipped" : "failed", detail: reason(r.error) };
    const job = r.job_id != null ? byId.get(r.job_id) : undefined;
    if (!job && jobs) return { work_id: r.work_id, title, state: "failed", detail: reason("not_found") };
    const st = job?.status ?? "queued";
    const state: ConvertRowState = st === "queued" ? "waiting" : st === "leased" ? "converting" : st === "succeeded" ? "done" : st;
    return { work_id: r.work_id, title, state, detail: state === "failed" ? reason(job?.error) : null };
  });
  return {
    rows,
    active: rows.some((x) => x.state === "waiting" || x.state === "converting"),
    done: rows.filter((x) => x.state === "done").length,
    failed: rows.filter((x, i) => x.state === "failed" && results[i].ok).length, // conversions that failed
  };
}
