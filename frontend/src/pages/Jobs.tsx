import { Fragment, useCallback, useEffect, useState } from "react";
import { EmptyState } from "../components/EmptyState";
import { Chevron, Pause, Play, Retry, Sync } from "../components/Icons";
import { PageTitle } from "../components/Shell";
import { get, post } from "../lib/api";
import { relativeTime } from "../lib/format";
import { useVisiblePolling } from "../lib/hooks";
import { GROUP_LABEL, GROUPS, batchSummary, bookLabel, groupItems, kindLabel, stateLabel, type JobQueue, type QueueBatch, type QueueRow } from "../lib/jobqueue";
import { useStore } from "../lib/store";

const BADGE: Record<string, string> = { running: "accent", waiting: "", paused: "warn", failed: "danger", done: "ok" };

/** Every background job: what runs, what waits and why, with cancel, pause, reorder, and retry (#87). */
export function JobsPage() {
  const { toast } = useStore();
  const [data, setData] = useState<JobQueue | null>(null);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const load = useCallback(() => get<JobQueue>("/api/job-queue").then(setData), []);
  useEffect(() => {
    load().catch((e) => toast((e as Error).message, "error"));
  }, [load, toast]);
  // The same 10 s poll as Admin → Workers, paused while the tab is hidden.
  useVisiblePolling(() => load().catch(() => {}), 10_000);

  const act = async (url: string, body: unknown, msg: string, confirm?: string) => {
    if (confirm && !window.confirm(confirm)) return;
    try {
      await post(url, body);
      toast(msg);
    } catch (e) {
      toast((e as Error).message, "error");
    }
    await load().catch(() => {});
  };
  const job = (j: QueueRow, verb: string, body: unknown, msg: string, confirm?: string) => act(`/api/job-queue/jobs/${j.id}/${verb}`, body, msg, confirm);

  if (!data) {
    return (
      <>
        <PageTitle>Jobs</PageTitle>
        <div className="hint" aria-busy="true">Loading…</div>
      </>
    );
  }
  const manage = data.can_manage;
  const items = groupItems(data);
  const empty = GROUPS.every((g) => items[g].length === 0);

  const actions = (j: QueueRow) => {
    const live = j.group === "running" || j.group === "waiting" || j.group === "paused";
    const label = `${kindLabel(j)}, ${bookLabel(j)}`;
    return (
      <div className="row job-actions">
        {manage && (j.group === "waiting" || j.group === "paused") && (
          <>
            <button className="btn sm ghost" onClick={() => job(j, "move", { to: "top" }, "Moved to the top")} aria-label={`Move to top: ${label}`} title="Move to top">Top</button>
            <button className="btn sm ghost" onClick={() => job(j, "move", { to: "bottom" }, "Moved to the bottom")} aria-label={`Move to bottom: ${label}`} title="Move to bottom">Bottom</button>
          </>
        )}
        {manage && j.group === "waiting" && (
          <button className="btn sm" onClick={() => job(j, "pause", {}, "Paused")} aria-label={`Pause: ${label}`}><Pause /> Pause</button>
        )}
        {manage && j.group === "paused" && (
          <button className="btn sm" onClick={() => job(j, "resume", {}, "Resumed")} aria-label={`Resume: ${label}`}><Play /> Resume</button>
        )}
        {manage && j.group === "failed" && (
          <button className="btn sm" onClick={() => job(j, "retry", {}, "Queued again")} aria-label={`Retry: ${label}`}><Retry /> Retry</button>
        )}
        {live && (manage || j.mine) && (
          <button
            className="btn sm danger"
            onClick={() => job(j, "cancel", {}, "Cancelled", j.group === "running" ? `Cancel ${kindLabel(j)} for “${bookLabel(j)}”? What it has done so far is discarded.` : undefined)}
            aria-label={`Cancel: ${label}`}
          >
            Cancel
          </button>
        )}
      </div>
    );
  };

  const jobRow = (j: QueueRow, nested = false) => (
    <tr key={j.id} data-group={j.group} data-testid={`job-${j.id}`} className={nested ? "nested" : undefined}>
      <td>
        <strong className={j.title ? undefined : "hint"}>{bookLabel(j)}</strong>
        <div className="hint">{kindLabel(j)}</div>
      </td>
      <td>
        <span className={`badge ${BADGE[j.group]}`}>{j.group}</span>
        <div className="hint job-state">{stateLabel(j)}</div>
        {j.group === "running" && j.progress != null && (
          <div className="progress-bar" role="progressbar" aria-label="Progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(j.progress * 100)}>
            <span style={{ width: `${Math.round(j.progress * 100)}%` }} />
          </div>
        )}
      </td>
      <td className="hint">{relativeTime(j.finished_at ?? j.started_at ?? j.created_at)}</td>
      <td>{actions(j)}</td>
    </tr>
  );

  const batchRow = (b: QueueBatch) => {
    const expanded = open.has(b.id);
    const live = b.counts.running + b.counts.waiting + b.counts.paused > 0;
    const toggle = () => setOpen((s) => { const n = new Set(s); if (n.has(b.id)) n.delete(b.id); else n.add(b.id); return n; });
    const name = `${kindLabel({ kind: b.kind, detail: null })}, ${b.total} ${b.total === 1 ? "book" : "books"}`;
    const url = `/api/job-queue/batches/${b.id}`;
    return (
      <Fragment key={b.id}>
        <tr data-group={b.group} data-testid={`batch-${b.id}`} className="batch">
          <td>
            <button className="btn ghost sm batch-toggle" aria-expanded={expanded} onClick={toggle}>
              <Chevron /> <strong>{name}</strong>
            </button>
            <div className="hint">Bulk request</div>
          </td>
          <td>
            <span className={`badge ${BADGE[b.group]}`}>{b.group}</span>
            <div className="hint">{batchSummary(b)}</div>
          </td>
          <td className="hint">{relativeTime(b.created_at)}</td>
          <td>
            <div className="row job-actions">
              {manage && b.counts.waiting > 0 && <button className="btn sm" onClick={() => act(`${url}/pause`, {}, "Paused the rest of the batch")} aria-label={`Pause rest: ${name}`}><Pause /> Pause rest</button>}
              {manage && b.counts.paused > 0 && <button className="btn sm" onClick={() => act(`${url}/resume`, {}, "Resumed the batch")} aria-label={`Resume: ${name}`}><Play /> Resume</button>}
              {live && (manage || b.mine) && (
                <button className="btn sm danger" onClick={() => act(`${url}/cancel`, {}, "Cancelled the rest of the batch", `Cancel the rest of this batch (${b.counts.running + b.counts.waiting + b.counts.paused} jobs)?`)} aria-label={`Cancel rest: ${name}`}>
                  Cancel rest
                </button>
              )}
            </div>
          </td>
        </tr>
        {expanded && b.jobs.map((j) => jobRow(j, true))}
      </Fragment>
    );
  };

  return (
    <>
      <PageTitle
        sub="Conversions, OCR, audiobooks, alignment, and transcodes, on this server or a worker"
        aside={manage ? (
          <button className="btn" onClick={() => act("/api/job-queue/pause", { paused: !data.queue_paused }, data.queue_paused ? "Queue resumed" : "Queue paused")}>
            {data.queue_paused ? <><Play /> Resume queue</> : <><Pause /> Pause queue</>}
          </button>
        ) : undefined}
      >
        Jobs
      </PageTitle>
      {data.queue_paused && (
        <div className="alert warn" role="status" style={{ marginBottom: 16 }}>
          The queue is paused: no new job starts. Running jobs finish.
        </div>
      )}
      {empty ? (
        <EmptyState icon={<Sync />} title="No jobs">
          {manage ? "Conversions, OCR, audiobooks, alignment, and transcodes show up here while they run." : "Jobs you start (conversions, audiobooks, OCR) show up here while they run."}
        </EmptyState>
      ) : (
        <div className="stack">
          {GROUPS.filter((g) => items[g].length).map((g) => (
            <section key={g} className="card flush job-group" aria-labelledby={`jobs-${g}`}>
              <h2 id={`jobs-${g}`}>{GROUP_LABEL[g]} <span className="hint">{items[g].length}</span></h2>
              <table className="list jobs-table">
                <thead>
                  <tr><th scope="col">Book</th><th scope="col">Status</th><th scope="col">When</th><th scope="col"><span className="sr-only">Actions</span></th></tr>
                </thead>
                <tbody>{items[g].map((it) => (it.type === "batch" ? batchRow(it.batch) : jobRow(it.job)))}</tbody>
              </table>
            </section>
          ))}
        </div>
      )}
    </>
  );
}
