import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { helpPath } from "../lib/help";
import { del, get, patch, post } from "../lib/api";
import { relativeTime } from "../lib/format";
import { useStore } from "../lib/store";
import { Copy, Plus } from "./Icons";
import { Sheet } from "./Sheet";

/** External workers (docs/mac-worker.md): who is online, what they run, and how to add one. */
export interface WorkerCredential {
  id: number;
  label: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  capabilities: string[] | null;
  advertised: string[];
  claimable: string[];
  state: "ready" | "busy" | "paused" | "off" | "offline" | "revoked";
  status_at: string | null;
  info: { platform?: string; version?: string; engines?: Record<string, string>; reason?: string; unavailable?: Record<string, string> };
  jobs: { id: number; kind: string; capability: string; work_id: number | null; progress: number | null; message: string | null }[];
}

/** Why an active job is where it is (lectern.queue.explain_wait). */
export interface JobWait {
  capability: string;
  summary: string;
  workers: { id: number; label: string; state: string; eligible: boolean; why: string | null }[];
  eligible_online: number;
  server_runs: boolean | null;
  ahead: number;
  lease: { holder: string; expires_at: string | null; heartbeat_at: string | null } | null;
  run_after: string | null;
  attempts: number;
  max_attempts: number;
  crashes: number;
  last_error: string | null;
}

export interface QueueJob {
  id: number;
  kind: string;
  capability: string;
  work_id: number | null;
  status: "queued" | "leased" | "succeeded" | "failed" | "cancelled";
  attempts: number;
  max_attempts: number;
  crashes: number;
  progress?: number | null;
  error: string | null;
  finished_at: string | null;
  waiting: JobWait | null;
}

interface Enrollment {
  id: number;
  label: string;
  code: string;
  command: string;
  expires_at: string;
}

const STATE_BADGE: Record<WorkerCredential["state"], string> = {
  ready: "ok",
  busy: "accent",
  paused: "",
  off: "",
  offline: "",
  revoked: "danger",
};

export function describeWorker(w: WorkerCredential): string {
  if (w.state === "busy" && w.jobs[0]) {
    const j = w.jobs[0];
    const pct = j.progress == null ? "" : ` ${Math.round(j.progress * 100)} %`;
    return `${j.capability} · work ${j.work_id ?? "?"}${pct}`;
  }
  if (w.state === "paused") return w.info.reason ?? "";
  if (w.state === "offline") return w.last_used_at ? `last seen ${relativeTime(w.last_used_at)}` : "never connected";
  return "";
}

export function WorkersCard() {
  const { toast } = useStore();
  const [rows, setRows] = useState<WorkerCredential[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [queue, setQueue] = useState<{ active: QueueJob[]; failed: QueueJob[] } | null>(null);
  const load = useCallback(
    () =>
      Promise.all([
        get<{ credentials: WorkerCredential[] }>("/api/worker-credentials").then((d) => setRows(d.credentials)),
        Promise.all([get<{ jobs: QueueJob[] }>("/api/jobs?status=active"), get<{ jobs: QueueJob[] }>("/api/jobs?status=failed")])
          .then(([a, f]) => setQueue({ active: a.jobs, failed: f.jobs.slice(0, 5) }))
          .catch(() => setQueue({ active: [], failed: [] })),
      ]),
    [],
  );
  useEffect(() => {
    load().catch(() => setRows([]));
    const t = window.setInterval(() => load().catch(() => {}), 10_000);
    return () => window.clearInterval(t);
  }, [load]);
  const revoke = async (w: WorkerCredential) => {
    try {
      await del(`/api/worker-credentials/${w.id}`);
      toast(`${w.label} revoked; its jobs go back to the queue`);
      await load();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  const limitAlign = async (w: WorkerCredential, only: string[] | null) => {
    try {
      await patch(`/api/worker-credentials/${w.id}`, { capabilities: only });
      await load();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  const active = (rows ?? []).filter((w) => !w.revoked_at);
  return (
    <section className="card" id="workers" aria-labelledby="workers-h" style={{ marginTop: 18 }}>
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "nowrap", alignItems: "flex-start" }}>
        <div>
          <h2 id="workers-h">Workers</h2>
          <p className="sub">
            Computers that align audiobooks, make generated audiobooks, and transcode for this server. A worker key can only run jobs: it cannot read your library or anyone’s progress. When no worker is online, this server runs what it can itself.
          </p>
        </div>
        <button className="btn" style={{ flexShrink: 0 }} onClick={() => setAdding(true)}><Plus /> Add a worker</button>
      </div>
      {rows === null ? (
        <div className="hint">Loading…</div>
      ) : active.length === 0 ? (
        <div className="hint">No workers yet. Add your Mac to align and make audiobooks on its GPU.</div>
      ) : (
        <table className="list">
          <thead><tr><th scope="col">Worker</th><th scope="col">Status</th><th scope="col">Runs</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>
            {active.map((w) => (
              <tr key={w.id}>
                <td><strong>{w.label}</strong><div className="hint">{[w.info.platform, w.info.version && `v${w.info.version}`].filter(Boolean).join(" · ") || `added ${relativeTime(w.created_at)}`}</div></td>
                <td><span className={`badge ${STATE_BADGE[w.state]}`}>{w.state}</span><div className="hint">{describeWorker(w)}</div></td>
                <td>
                  {w.claimable.length ? w.claimable.join(", ") : <span className="hint">—</span>}
                  {w.capabilities && <div className="hint">limited to {w.capabilities.join(", ")}</div>}
                  {Object.entries(w.info.unavailable ?? {}).map(([cap, why]) => (
                    <div key={cap} className="hint" title={why}>not offering {cap}: {why}</div>
                  ))}
                </td>
                <td style={{ textAlign: "right" }}>
                  <div className="row" style={{ justifyContent: "flex-end", flexWrap: "nowrap" }}>
                    {w.capabilities ? (
                      <button className="btn sm" onClick={() => limitAlign(w, null)}>Allow all</button>
                    ) : (
                      <button className="btn sm" onClick={() => limitAlign(w, ["align"])}>Only align</button>
                    )}
                    <button className="btn sm danger" onClick={() => revoke(w)}>Revoke</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {queue && <JobQueue active={queue.active} failed={queue.failed} />}
      <p className="hint" style={{ marginTop: 12 }}>
        <Link to="/app/jobs">Open the job queue</Link> to pause, reorder, retry, or cancel jobs.
      </p>
      <Sheet open={adding} onClose={() => setAdding(false)} title="Add a worker" variant="dialog" labelledBy="addw-title">
        {adding && <AddWorker onDone={() => { setAdding(false); load(); }} />}
      </Sheet>
    </section>
  );
}

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

/** Waiting and running jobs with why they wait, and the latest failures with their reason. */
export function JobQueue({ active, failed }: { active: QueueJob[]; failed: QueueJob[] }) {
  if (!active.length && !failed.length) return null;
  return (
    <div className="stack" style={{ marginTop: 16 }} aria-label="Job queue">
      {active.length > 0 && <h3 style={{ margin: 0, fontSize: 15 }}>Queue</h3>}
      {active.map((j) => {
        const w = j.waiting;
        return (
          <div key={j.id} className="queue-job" data-status={j.status}>
            <div className="row" style={{ gap: 8, flexWrap: "nowrap", alignItems: "baseline" }}>
              <span className={`badge ${j.status === "leased" ? "accent" : ""}`}>{j.status === "leased" ? "running" : "queued"}</span>
              <strong>Job {j.id}</strong>
              <span className="hint">{j.capability}{j.work_id != null ? ` · work ${j.work_id}` : ""}</span>
            </div>
            <div className="hint" data-testid="wait-summary">{w?.summary ?? ""}</div>
            {w && (
              <details className="tech">
                <summary>Why it is waiting</summary>
                <dl className="kv">
                  <dt>Needs</dt>
                  <dd>a worker that offers <code>{w.capability}</code>{w.server_runs ? " (this server also runs it itself)" : ""}</dd>
                  <dt>Workers</dt>
                  <dd>
                    {w.workers.length === 0 ? "none added" : (
                      <ul style={{ margin: 0, paddingLeft: 18 }}>
                        {w.workers.map((x) => <li key={x.id}>{x.label}: {x.eligible ? "can take it" : x.why}</li>)}
                      </ul>
                    )}
                  </dd>
                  {w.lease && (<><dt>Lease</dt><dd>{w.lease.holder}, expires {when(w.lease.expires_at)}, last heartbeat {when(w.lease.heartbeat_at)}</dd></>)}
                  <dt>Runs after</dt>
                  <dd>{when(w.run_after)}{w.ahead ? ` · ${w.ahead} ahead in line` : ""}</dd>
                  <dt>Attempts</dt>
                  <dd>{w.attempts} of {w.max_attempts}{w.crashes ? ` · crashed its worker ${w.crashes}×` : ""}</dd>
                  {w.last_error && (<><dt>Last error</dt><dd>{w.last_error}</dd></>)}
                </dl>
              </details>
            )}
          </div>
        );
      })}
      {failed.length > 0 && <h3 style={{ margin: "8px 0 0", fontSize: 15 }}>Recently failed</h3>}
      {failed.map((j) => (
        <div key={j.id} className="queue-job" data-status="failed">
          <div className="row" style={{ gap: 8, flexWrap: "nowrap", alignItems: "baseline" }}>
            <span className="badge danger">failed</span>
            <strong>Job {j.id}</strong>
            <span className="hint">{j.capability}{j.work_id != null ? ` · work ${j.work_id}` : ""} · {when(j.finished_at)}</span>
          </div>
          <div className="hint">{j.error ?? "no reason recorded"}</div>
        </div>
      ))}
    </div>
  );
}

function AddWorker({ onDone }: { onDone: () => void }) {
  const { toast } = useStore();
  const [label, setLabel] = useState("");
  const [made, setMade] = useState<Enrollment | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  if (made) {
    return (
      <div className="stack">
        <p className="sub" style={{ margin: 0 }}>
          On the Mac, open Terminal and run this. It installs the worker and its menu-bar app and connects to this server. The code works once, for an hour.
        </p>
        <div className="secret"><code>{made.command}</code></div>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button
            className="btn"
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(made.command);
                setCopied(true);
              } catch {
                toast("Copy failed; select the command instead.", "error");
              }
            }}
          >
            <Copy /> {copied ? "Copied" : "Copy command"}
          </button>
          <button className="btn primary" onClick={onDone} data-autofocus>Done</button>
        </div>
        <p className="hint">Behind a school or office proxy? Add <code style={{ whiteSpace: "nowrap" }}>--proxy http://proxy:port</code> (and <code style={{ whiteSpace: "nowrap" }}>--ca-bundle</code> if it inspects HTTPS). See <Link to={helpPath("workerProxy")}>school and proxy mode</Link>.</p>
      </div>
    );
  }
  return (
    <form
      onSubmit={async (e) => {
        e.preventDefault();
        try {
          setMade(await post<Enrollment>("/api/worker-enrollments", { label }));
        } catch (ex) {
          setErr((ex as Error).message);
        }
      }}
    >
      {err && <div className="alert error" role="alert" style={{ marginBottom: 12 }}>{err}</div>}
      <label className="field"><span>Name</span><input className="input" value={label} onChange={(e) => setLabel(e.target.value)} maxLength={128} required placeholder="e.g. Jen’s MacBook" data-autofocus /></label>
      <div className="row" style={{ justifyContent: "flex-end" }}><button className="btn primary"><Plus /> Create install command</button></div>
    </form>
  );
}
