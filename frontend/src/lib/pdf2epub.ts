// PDF → EPUB conversion (issue #28): turn a work's text PDF into a reflowable EPUB edition,
// with the PDF's own page numbers kept as metadata (a page-list nav the Kindle and KOReader
// can show). The original PDF is kept; the EPUB is a separate edition.

import { del, get, post } from "./api";

export interface PdfEpubReport {
  pdf_sha256: string | null;
  status: string;
  pages: number;
  sections: number;
  page_list_entries: number;
  labels_source: "page_labels" | "physical";
  warnings: string[];
  pages_without_text: number[];
  unlinked_pages: number[];
}

export interface PdfEpubJob {
  id: number;
  status: "queued" | "leased" | "succeeded" | "failed" | "cancelled";
  error: string | null;
  sections: number | null;
}

export interface PdfEpubState {
  work_id: number;
  eligible: boolean;
  reason: string | null;
  job: PdfEpubJob | null;
  edition: { file: string; size: number; sha256: string; added_at: string } | null;
  report: PdfEpubReport | null;
}

export const pdfEpubState = (workId: number) => get<PdfEpubState>(`/api/works/${workId}/pdf-epub`);
export const convertPdf = (workId: number) =>
  post<{ status: string; job_id: number }>(`/api/works/${workId}/pdf-epub`);
export const retryPdf = (workId: number) =>
  post<{ status: string; job_id: number }>(`/api/works/${workId}/pdf-epub/retry`);
export const removePdfEpub = (workId: number) => del<{ removed: boolean }>(`/api/works/${workId}/pdf-epub`);
export const cancelPdfJob = (jobId: number) => post<unknown>(`/api/jobs/${jobId}/cancel`);

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
 * and the latest job states (`jobs`; a job not in it yet stays "waiting"). `active`: some
 * conversion is still queued or running, so keep polling. */
export function convertProgress(results: BulkConvertResult[], jobs: BulkConvertJob[]): { rows: ConvertRow[]; active: boolean; done: number; failed: number } {
  const byId = new Map(jobs.map((j) => [j.id, j]));
  const rows = results.map((r): ConvertRow => {
    const title = r.title ?? `Book ${r.work_id}`;
    if (!r.ok) return { work_id: r.work_id, title, state: r.status === "skipped" ? "skipped" : "failed", detail: reason(r.error) };
    const job = r.job_id != null ? byId.get(r.job_id) : undefined;
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
