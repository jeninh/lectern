import { describe, expect, it } from "vitest";
import { convertProgress, convertSummary, type BulkConvertJob, type BulkConvertResult } from "../pdf2epub";

const results: BulkConvertResult[] = [
  { work_id: 1, title: "A", ok: true, status: "queued", job_id: 10 },
  { work_id: 2, title: "B", ok: true, status: "already_queued", job_id: 11 },
  { work_id: 3, title: "C", ok: false, status: "skipped", error: "already_epub" },
  { work_id: 4, title: "D", ok: false, status: "skipped", error: "already_epub" },
  { work_id: 5, title: "E", ok: false, status: "skipped", error: "no_text_pdf" },
  { work_id: 6, ok: false, status: "failed", error: "not_found" },
  { work_id: 7, title: "G", ok: true, status: "queued", job_id: 12 },
];
const job = (id: number, work_id: number, status: BulkConvertJob["status"], error: string | null = null): BulkConvertJob => ({ id, work_id, status, error, sections: null });

describe("convertSummary", () => {
  it("counts queued, skipped, and failed books with each reason once", () => {
    const s = convertSummary(results);
    expect(s).toMatchObject({ queued: 3, skipped: 3, failed: 1 });
    expect(s.text).toBe(
      "Queued EPUB conversion for 3 books; skipped 3 books (already an EPUB; no text in the PDF yet (a scan needs OCR first)); 1 book failed (no longer exists).",
    );
  });
  it("says so when nothing could be queued", () => {
    const s = convertSummary([{ work_id: 1, ok: false, status: "skipped", error: "unsupported" }]);
    expect(s.queued).toBe(0);
    expect(s.text).toBe("Queued EPUB conversion for 0 books; skipped 1 book (no PDF to convert).");
  });
});

describe("convertProgress", () => {
  it("follows each queued book's job and keeps skipped and failed books with their reason", () => {
    const p = convertProgress(results, [job(10, 1, "succeeded"), job(11, 2, "leased"), job(12, 7, "failed", "pdf_changed")]);
    expect(p.rows.map((r) => [r.work_id, r.state, r.detail])).toEqual([
      [1, "done", null],
      [2, "converting", null],
      [3, "skipped", "already an EPUB"],
      [4, "skipped", "already an EPUB"],
      [5, "skipped", "no text in the PDF yet (a scan needs OCR first)"],
      [6, "failed", "no longer exists"],
      [7, "failed", "the PDF changed while it was queued"],
    ]);
    expect(p.rows[5].title).toBe("Book 6");
    // a refused book (6) is not a failed conversion; only job 12 is
    expect(p).toMatchObject({ active: true, done: 1, failed: 1 });
  });
  it("treats a job not reported yet as waiting, and stops once every job has finished", () => {
    expect(convertProgress(results, null).rows[0].state).toBe("waiting");
    const p = convertProgress(results, [job(10, 1, "succeeded"), job(11, 2, "cancelled"), job(12, 7, "succeeded")]);
    expect(p).toMatchObject({ active: false, done: 2, failed: 0 });
    expect(p.rows[1].state).toBe("cancelled");
  });
  it("treats a job missing from an answer as gone instead of waiting forever", () => {
    const p = convertProgress(results, [job(10, 1, "succeeded"), job(11, 2, "succeeded")]);
    expect([p.rows[6].state, p.rows[6].detail]).toEqual(["failed", "no longer exists"]);
    expect(p).toMatchObject({ active: false, done: 2, failed: 1 });
  });
});
