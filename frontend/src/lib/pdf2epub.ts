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
