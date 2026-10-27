import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../lib/api";
import {
  analysePdf,
  cancelPdfJob,
  convertHint,
  convertPdf,
  editionHint,
  pdfEpubState,
  removePdfEpub,
  retryPdf,
  type PdfEpubState,
  type PdfRules,
} from "../lib/pdf2epub";
import { useStore } from "../lib/store";
import type { Work } from "../lib/types";
import { Book } from "./Icons";
import { PdfReviewSheet } from "./PdfReviewSheet";

const POLL_MS = 3000;

/**
 * "Convert to EPUB" row for a work with a text PDF: analyses the PDF (a quick job), opens the
 * review sheet with the detected rules and a side-by-side preview, queues the conversion with
 * the confirmed rules, follows the job (progress, Cancel, Retry), and once the edition exists,
 * offers "Edit rules…" (re-convert) and removal. A scan converts once OCR has given it text;
 * when the PDF's text changed since the conversion (the scan was recognised again), the row
 * says the edition is out of date and offers "Convert again…" (analysis of the new text, then
 * the review with the saved rules).
 */
export function ConvertPdfRow({ work }: { work: Work }) {
  const { refreshLibrary, toast } = useStore();
  const [st, setSt] = useState<PdfEpubState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const wasRunning = useRef(false);
  const [review, setReview] = useState(false);
  const reviewAfterAnalysis = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const next = await pdfEpubState(work.id);
      setSt(next);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [work.id]);

  useEffect(() => {
    refresh();
  }, [refresh]);
  const job = st?.job;
  const running = job?.status === "queued" || job?.status === "leased";
  // Side effects live in an effect on the succeeded transition, not inside a setSt
  // updater (React may invoke updaters twice in development).
  useEffect(() => {
    if (wasRunning.current && !running && job?.status === "succeeded") {
      if (job.mode === "analyse") {
        if (reviewAfterAnalysis.current) setReview(true);
        reviewAfterAnalysis.current = false;
      } else if (st?.edition) {
        toast(`The converted EPUB of ${work.title} is ready.`);
        refreshLibrary().catch(() => {});
      }
    }
    wasRunning.current = running;
  });
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(refresh, POLL_MS);
    return () => window.clearInterval(id);
  }, [running, refresh]);

  if (!st) return null;
  if (!st.edition && !job && st.reason === "no_text_pdf") return null;

  const act = async (fn: () => Promise<unknown>, fail: string, after?: () => void) => {
    try {
      await fn();
      await refresh();
      after?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : fail);
    }
  };

  // Review first: open the sheet when the analysis is current, else analyse and open it after.
  const startReview = () => {
    if (st.analysed) {
      setReview(true);
      return;
    }
    reviewAfterAnalysis.current = true;
    act(() => analysePdf(work.id), "Could not analyse the PDF");
  };
  const confirm = async (rules: PdfRules) => {
    await convertPdf(work.id, { rules, reconvert: !!st.edition });
    setReview(false);
    await refresh();
  };

  let hint = convertHint(st.source);
  let action = (
    <button className="btn sm" onClick={startReview}>
      Convert to EPUB…
    </button>
  );
  if (running) {
    const analysing = job!.mode === "analyse";
    hint = job!.status === "queued" ? (analysing ? "Queued: analysing the PDF" : "Queued: converting") : analysing ? "Analysing the PDF…" : "Converting…";
    action = <button className="btn sm ghost" onClick={() => act(() => cancelPdfJob(job!.id), "Could not cancel")}>Cancel</button>;
  } else if (job && (job.status === "failed" || job.status === "cancelled")) {
    const what = job.mode === "analyse" ? "Analysis" : "Conversion";
    hint = job.status === "cancelled" ? `${what} cancelled` : `${what} failed: ${job.error ?? "unknown error"}`;
    action = (
      <button className="btn sm ghost" onClick={() => act(() => retryPdf(work.id), "Could not retry")}>
        Retry
      </button>
    );
  } else if (st.edition) {
    hint = editionHint(st);
    action = (
      <div className="slot-actions">
      <button className={`btn sm${st.outdated ? "" : " ghost"}`} onClick={startReview}>
        {st.outdated ? "Convert again…" : "Edit rules…"}
      </button>
      <button
        className="btn sm ghost"
        onClick={() =>
          act(
            () => removePdfEpub(work.id),
            "Could not remove",
            () => {
              toast(`The converted EPUB of ${work.title} was removed.`);
              refreshLibrary().catch(() => {});
            },
          )
        }
      >
        Remove
      </button>
      </div>
    );
  }

  return (
    <li className="slot" data-state={job?.status ?? (st.edition ? "ready" : "none")}>
      <Book />
      <span>
        <strong>EPUB from PDF</strong>{" "}
        <span className="hint" aria-live="polite">
          {hint}
          {error && ` · ${error}`}
        </span>
        {st.report && st.report.warnings.length > 0 && !running && (
          <div className="hint">{st.report.warnings.join(" · ")}</div>
        )}
      </span>
      {action}
      {review && (
        <PdfReviewSheet workId={work.id} title={work.title} open={review} onClose={() => setReview(false)} onConvert={confirm} reconvert={!!st.edition} />
      )}
    </li>
  );
}
