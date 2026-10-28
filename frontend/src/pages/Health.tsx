import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { PageTitle } from "../components/Shell";
import { get, post } from "../lib/api";
import { absoluteTime, bytes, relativeTime } from "../lib/format";
import { GROUPS, groupChecks, LEVEL_BADGE, LEVEL_LABEL, pushLine, retryRequest, type HealthCheck, type HealthJob, type HealthLevel, type HealthView, type PushInfo } from "../lib/health";
import { useVisiblePolling } from "../lib/hooks";
import { useStore } from "../lib/store";

function Level({ level }: { level: HealthLevel }) {
  return <span className={`badge ${LEVEL_BADGE[level]}`}>{level === "info" ? "Info" : LEVEL_LABEL[level]}</span>;
}

function JobList({ items, total, onRetry }: { items: HealthJob[]; total: number; onRetry: (j: HealthJob) => void }) {
  if (!items.length) return null;
  return (
    <ul className="health-jobs">
      {items.map((j) => (
        <li key={j.id}>
          <div>
            <strong>{j.label}</strong>
            {j.work_id && <> · <Link to={`/app/library/${j.work_id}`}>{j.work_title ?? `Book ${j.work_id}`}</Link></>}
            {j.at && <span className="hint"> · <time title={absoluteTime(j.at)}>{relativeTime(j.at)}</time></span>}
            {j.error && <div className="hint">{j.error}</div>}
          </div>
          {retryRequest(j) && <button type="button" className="btn sm" onClick={() => onRetry(j)}>Retry</button>}
        </li>
      ))}
      {total > items.length && <li className="hint">…and {total - items.length} more</li>}
    </ul>
  );
}

function CheckRow({ c, since, onRetry, onTestPush, testing }: { c: HealthCheck; since?: string | null; onRetry: (j: HealthJob) => void; onTestPush: () => void; testing: boolean }) {
  const d = c.detail;
  const push = c.id === "alerts.push" ? (d as unknown as PushInfo) : null;
  return (
    <li className="health-check" data-level={c.level}>
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "nowrap", gap: 12 }}>
        <div>
          <strong>{c.title}</strong>
          <div className="hint">
            {c.summary}
            {since && c.level !== "ok" && c.level !== "info" && <> · since <time title={absoluteTime(since)}>{relativeTime(since)}</time></>}
          </div>
        </div>
        <Level level={c.level} />
      </div>
      {typeof d.last_error === "string" && <div className="alert error" style={{ marginTop: 8 }}>{d.last_error}</div>}
      {typeof d.free_bytes === "number" && typeof d.total_bytes === "number" && (
        <div className="hint">
          {bytes(d.free_bytes)} free of {bytes(d.total_bytes)} · amber below {String(d.warn_percent)}%, red below {String(d.critical_percent)}% · <code>{(d.paths as string[]).join(", ")}</code>
        </div>
      )}
      {typeof d.reason === "string" && <div className="hint">{d.reason}</div>}
      {d.items && <JobList items={d.items} total={d.total ?? d.items.length} onRetry={onRetry} />}
      {push && (
        <div className="row" style={{ justifyContent: "space-between", marginTop: 6 }}>
          <span className="hint">
            {pushLine(push)}
            {push.last?.at && <> Last push {relativeTime(push.last.at)}: {push.last.ok ? "delivered" : `not delivered (${push.last.error})`}.</>}
          </span>
          {push.state === "on" && <button type="button" className="btn sm" onClick={onTestPush} disabled={testing}>{testing ? "Sending…" : "Send a test alert"}</button>}
        </div>
      )}
    </li>
  );
}

/** Owner-only Health page: backups, disk space, jobs, workers, and the rest, with one status. */
export function HealthPage() {
  const { toast } = useStore();
  const [view, setView] = useState<HealthView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const load = useCallback(() => get<HealthView>("/api/admin/health").then((v) => { setView(v); setError(null); }, (e) => setError((e as Error).message)), []);
  useEffect(() => {
    load();
  }, [load]);
  // The page shows what the server's 5-minute check stored, so this is a row read on a Pi.
  useVisiblePolling(load, 60000);
  const checkNow = async () => {
    try {
      setView(await post<HealthView>("/api/admin/health/check"));
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  const retry = async (j: HealthJob) => {
    const r = retryRequest(j);
    if (!r) return;
    try {
      await post(r.path, r.body);
      toast(`${j.label} queued again.`);
      await checkNow();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  const testPush = async () => {
    setTesting(true);
    try {
      const r = await post<{ ok: boolean; error: string | null }>("/api/admin/health/push-test");
      toast(r.ok ? "Test alert sent." : `The test alert was not delivered (${r.error}).`, r.ok ? undefined : "error");
      await checkNow();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setTesting(false);
    }
  };
  const groups = view ? groupChecks(view.checks) : [];
  return (
    <div className="page">
      <PageTitle
        sub={view ? <>Checked <time title={absoluteTime(view.checked_at)}>{relativeTime(view.checked_at)}</time></> : undefined}
        aside={<button type="button" className="btn" onClick={checkNow}>Check now</button>}
      >
        Health
      </PageTitle>
      {error && <div className="alert error" role="alert">{error}</div>}
      {!view && !error && <div className="hint">Loading…</div>}
      {view && (
        <>
          <section className="card health-overall" data-level={view.level} aria-live="polite">
            <h2>{view.level === "ok" ? "Everything looks fine" : view.level === "warning" ? "Something needs a look" : "Something is wrong"}</h2>
            <p className="sub">
              Owners get an alert in the bell when a check turns amber or red, and when it recovers (at most once a day each). <Link to="/app/docs/health">How it works</Link>
            </p>
            {view.offline && (
              <div className="alert warn" style={{ marginTop: 8 }}>
                Offline mode is on: Lectern attempts no outbound connection, so push alerts are paused and alerts stay in the app.
              </div>
            )}
          </section>
          {groups.map((g) => (
            <section key={g.id} className="card" aria-labelledby={`h-${g.id}`} style={{ marginTop: 18 }}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <h2 id={`h-${g.id}`}>{GROUPS.find((x) => x.id === g.id)?.title}</h2>
                <Level level={g.level} />
              </div>
              <ul className="health-list">
                {g.checks.map((c) => (
                  <CheckRow key={c.id} c={c} since={view.since[c.id]} onRetry={retry} onTestPush={testPush} testing={testing} />
                ))}
              </ul>
              {g.id === "backups" && <p className="hint"><Link to="/app/admin#backups">Admin → Nightly backups</Link></p>}
              {g.id === "workers" && view.workers.length > 0 && (
                <table className="list" style={{ marginTop: 10 }}>
                  <thead><tr><th scope="col">Worker</th><th scope="col">State</th><th scope="col">Last seen</th><th scope="col">Offers</th></tr></thead>
                  <tbody>
                    {view.workers.map((w) => (
                      <tr key={w.id}>
                        <td><strong>{w.label}</strong>{w.version && <div className="hint">{w.version}</div>}</td>
                        <td><span className={`badge ${w.state === "ready" || w.state === "busy" ? "ok" : w.state === "offline" ? "warn" : ""}`}>{w.state}</span></td>
                        <td>{w.seen_at ? <time title={absoluteTime(w.seen_at)}>{relativeTime(w.seen_at)}</time> : <span className="hint">never</span>}</td>
                        <td>
                          {w.capabilities.join(", ") || <span className="hint">nothing yet</span>}
                          {Object.entries(w.unavailable).map(([cap, why]) => <div key={cap} className="hint">{cap}: {why}</div>)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>
          ))}
        </>
      )}
    </div>
  );
}

/** Top-bar badge for owners: hidden while everything is fine. */
export function HealthBadge() {
  const { me } = useStore();
  const owner = me?.user.role === "owner";
  const [level, setLevel] = useState<HealthLevel | null>(null);
  const load = useCallback(() => {
    if (owner) get<{ level: HealthLevel | null }>("/api/admin/health/summary").then((s) => setLevel(s.level), () => {});
  }, [owner]);
  useEffect(() => {
    load();
  }, [load]);
  useVisiblePolling(load, 120000);
  if (!owner || !level || level === "ok" || level === "info") return null;
  return (
    <Link to="/app/health" className="net-status health-badge" data-level={level} aria-label={`Health: ${LEVEL_LABEL[level]}`} title="Open Health">
      <span className="dot" aria-hidden="true" />
      {level === "critical" ? "Critical" : "Warning"}
    </Link>
  );
}
