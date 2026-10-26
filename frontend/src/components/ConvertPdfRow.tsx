import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../lib/api";
import {
  cancelPdfJob,
  convertPdf,
  pdfEpubState,
  removePdfEpub,
  retryPdf,
  type PdfEpubState,
} from "../lib/pdf2epub";
import { useStore } from "../lib/store";
import type { Work } from "../lib/types";
import { Book } from "./Icons";

const POLL_MS = 3000;

/**
 * "Convert to EPUB" row for a work with a text PDF: queues the conversion, follows the job
 * (progress, Cancel, Retry), and once it exists, offers the converted edition for removal.
 */
export function ConvertPdfRow({ work }: { work: Work }) {
  const { refreshLibrary, toast } = useStore();
  const [st, setSt] = useState<PdfEpubState | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await pdfEpubState(work.id);
      setSt((prev) => {
        if (prev?.job && prev.job.status !== "succeeded" && next.job?.status === "succeeded" && next.edition) {
          toast(`The converted EPUB of ${work.title} is ready.`);
          refreshLibrary().catch(() => {});
        }
        return next;
      });
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [work.id, work.title, refreshLibrary, toast]);

  useEffect(() => {
    refresh();
  }, [refresh]);
  const job = st?.job;
  const running = job?.status === "queued" || job?.status === "leased";
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

  let hint = "Reflowable text, the PDF's own page numbers kept; the PDF stays";
  let action = (
    <button className="btn sm" onClick={() => act(() => convertPdf(work.id), "Could not start the conversion")}>
      Convert to EPUB…
    </button>
  );
  if (running) {
    hint = job!.status === "queued" ? "Queued: converting" : "Converting…";
    action = <button className="btn sm ghost" onClick={() => act(() => cancelPdfJob(job!.id), "Could not cancel")}>Cancel</button>;
  } else if (job && (job.status === "failed" || job.status === "cancelled")) {
    hint = job.status === "cancelled" ? "Conversion cancelled" : `Conversion failed: ${job.error ?? "unknown error"}`;
    action = (
      <button className="btn sm ghost" onClick={() => act(() => retryPdf(work.id), "Could not retry")}>
        Retry
      </button>
    );
  } else if (st.edition) {
    hint = `EPUB converted from the PDF · ${st.report?.sections ?? 0} section${
      st.report?.sections === 1 ? "" : "s"
    }${st.report?.page_list_entries ? `, page numbers ${st.report.labels_source === "page_labels" ? "from the PDF's page labels" : "1…" + st.report.pages}` : ""}`;
    action = (
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
    </li>
  );
}
