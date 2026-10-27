import { useEffect, useState } from "react";
import { bulkConvertJobs, convertProgress, type BulkConvertJob, type BulkConvertResult, type ConvertRowState } from "../lib/pdf2epub";
import { useStore } from "../lib/store";
import { Sheet } from "./Sheet";

const POLL_MS = 3000;
const LABEL: Record<ConvertRowState, string> = {
  waiting: "Waiting",
  converting: "Converting…",
  done: "Converted",
  failed: "Failed",
  cancelled: "Cancelled",
  skipped: "Skipped",
};

/** Per-book progress of a bulk "Convert to EPUB": every selected book with its outcome, the
 * queued ones followed through the job queue (one conversion runs at a time on the server)
 * while the sheet is open. */
export function BulkConvertSheet({ results, onClose }: { results: BulkConvertResult[] | null; onClose: () => void }) {
  const { refreshLibrary } = useStore();
  const [jobs, setJobs] = useState<BulkConvertJob[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const ids = (results ?? []).flatMap((r) => (r.ok && r.job_id != null ? [r.job_id] : []));
  const key = ids.join(",");
  const { rows, active, done, failed } = convertProgress(results ?? [], jobs);

  useEffect(() => {
    setJobs(null);
    if (!key) return;
    let stop = false;
    let timer = 0;
    let finished = false;
    const tick = async () => {
      try {
        const r = await bulkConvertJobs(key.split(",").map(Number));
        if (stop) return;
        setJobs(r.jobs);
        setError(null);
        finished = !convertProgress(results ?? [], r.jobs).active;
      } catch (e) {
        if (!stop) setError((e as Error).message);
      }
      if (finished) refreshLibrary().catch(() => {});
      else if (!stop) timer = window.setTimeout(tick, POLL_MS);
    };
    tick();
    return () => {
      stop = true;
      window.clearTimeout(timer);
    };
    // `results` changes identity only with a new batch, which changes `key` too.
  }, [key]);

  const queued = ids.length;
  return (
    <Sheet open={results !== null} onClose={onClose} title="Convert to EPUB" variant="dialog" labelledBy="bulk-convert-title">
      <div className="stack" data-testid="bulk-convert">
        <p className="hint" style={{ margin: 0 }} aria-live="polite">
          {queued
            ? active
              ? `Converted ${done} of ${queued}${failed ? `, ${failed} failed` : ""}. Conversions run one at a time on the server; you can close this.`
              : `Finished: ${done} of ${queued} converted${failed ? `, ${failed} failed (retry from the book's details)` : ""}.`
            : "Nothing was queued."}
          {error && ` · ${error}`}
        </p>
        <ul className="list" style={{ maxHeight: "50vh", overflow: "auto", margin: 0, padding: 0, listStyle: "none" }}>
          {rows.map((r) => (
            <li key={r.work_id} className="row" data-state={r.state} style={{ justifyContent: "space-between", gap: "1rem" }}>
              <span>{r.title}</span>
              <span className="hint" style={{ textAlign: "right" }}>
                {LABEL[r.state]}
                {r.detail && `: ${r.detail}`}
              </span>
            </li>
          ))}
        </ul>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button className="btn" onClick={onClose} data-autofocus>Close</button>
        </div>
      </div>
    </Sheet>
  );
}
