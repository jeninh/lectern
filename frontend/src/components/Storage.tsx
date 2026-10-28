import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { get, post } from "../lib/api";
import { absoluteTime, bytes, relativeTime } from "../lib/format";
import { useVisiblePolling } from "../lib/hooks";
import { useStore } from "../lib/store";
import {
  ACTIONS,
  BLOCKED_TEXT,
  type Action,
  canConfirm,
  categoryRows,
  confirmLabel,
  freeTrend,
  jobActive,
  jobText,
  type Preview,
  preRestoreDate,
  selection,
  type StorageView,
  trendText,
} from "../lib/storage";
import { OverflowMenu } from "./Menu";
import { Sheet } from "./Sheet";

interface Ask {
  action: Action;
  params: { older_than_days?: number; work_id?: number; name?: string };
  title: string;
}

const VOLUME_NAMES: Record<string, string> = { data: "Data", library: "Library", backups: "Backup target" };

/** Owner-only: where the disk goes, and cleanup of what Lectern can make again (docs/storage.md). */
export function StorageCard() {
  const { toast } = useStore();
  const [view, setView] = useState<StorageView | null>(null);
  const [ask, setAsk] = useState<Ask | null>(null);
  const [days, setDays] = useState(0);
  const load = useCallback(() => get<StorageView>("/api/admin/storage").then(setView), []);
  useEffect(() => {
    load().catch(() => {});
  }, [load]);
  const wasActive = useRef(false);
  useVisiblePolling(() => {
    if (view && (view.scanning || jobActive(view.job))) load().catch(() => {});
  }, 2500);
  useEffect(() => {
    // A cleanup just finished: measure again so the totals show what it freed.
    const active = jobActive(view?.job ?? null);
    if (wasActive.current && !active) post<StorageView>("/api/admin/storage/scan").then(setView, () => {});
    wasActive.current = active;
  }, [view?.job]);
  const rescan = async () => {
    try {
      setView(await post<StorageView>("/api/admin/storage/scan"));
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  if (!view) {
    return (
      <section className="card" id="storage" style={{ marginTop: 18 }}>
        <h2>Storage</h2>
        <div className="hint">Loading…</div>
      </section>
    );
  }
  const scan = view.scan;
  const running = jobActive(view.job);
  return (
    <section className="card" id="storage" aria-labelledby="st-h" style={{ marginTop: 18 }}>
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "nowrap" }}>
        <div>
          <h2 id="st-h">Storage</h2>
          <p className="sub">
            Where the disk space goes, measured in the background every few hours, and cleanup of files Lectern can make again. Originals are never deleted here.{" "}
            {scan ? <>Measured <time title={absoluteTime(scan.scanned_at)}>{relativeTime(scan.scanned_at)}</time>.</> : null}
          </p>
        </div>
        <button className="btn sm" onClick={rescan} disabled={view.scanning}>{view.scanning ? "Measuring…" : "Measure now"}</button>
      </div>

      {view.blocked && <div className="alert warn" role="status" style={{ marginBottom: 12 }}>{BLOCKED_TEXT[view.blocked]}</div>}

      <ul className="storage-volumes" aria-label="Free space">
        {view.volumes.map((v) => {
          const trend = trendText(freeTrend(view.history, v.id));
          return (
            <li key={v.id} className={v.low ? "low" : ""}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <strong>{v.roles.map((r) => VOLUME_NAMES[r] ?? r).join(" + ")}</strong>
                <span>{bytes(v.free)} free of {bytes(v.total)}{trend ? <span className="hint"> · {trend}</span> : null}</span>
              </div>
              <div className="meter" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(100 - v.free_percent)} aria-label={`${VOLUME_NAMES[v.id] ?? v.id} used`}>
                <span style={{ width: `${Math.min(100, 100 - v.free_percent)}%` }} />
              </div>
              {v.low && <div className="field-error" role="alert">Less than {view.warn_free_percent}% free.</div>}
            </li>
          );
        })}
      </ul>

      {!scan ? (
        <p className="hint">{view.scanning ? "Measuring for the first time…" : "Not measured yet."}</p>
      ) : (
        <>
          <table className="list storage-cats">
            <thead>
              <tr><th scope="col">What</th><th scope="col">Size</th><th scope="col">Files</th><th scope="col"><span className="sr-only">Share</span></th></tr>
            </thead>
            <tbody>
              {categoryRows(scan).map((c) => (
                <tr key={c.key} data-category={c.key}>
                  <td><strong>{c.label}</strong><div className="hint">{c.hint}</div></td>
                  <td>{bytes(c.bytes)}</td>
                  <td>{c.files}</td>
                  <td style={{ width: "30%" }}><div className="meter thin" aria-hidden="true"><span style={{ width: `${(c.share * 100).toFixed(1)}%` }} /></div></td>
                </tr>
              ))}
            </tbody>
          </table>
          {scan.skipped_links > 0 && <p className="hint">{scan.skipped_links} symbolic {scan.skipped_links === 1 ? "link was" : "links were"} not followed or counted.</p>}

          {scan.largest_works.length > 0 && (
            <>
              <h3 style={{ marginTop: 16 }}>Largest books</h3>
              <table className="list">
                <tbody>
                  {scan.largest_works.map((w) => (
                    <tr key={w.id}>
                      <td>{w.trashed ? <>{w.title} <span className="badge">in Trash</span></> : <Link to={`/app/library/${w.id}`}>{w.title}</Link>}<div className="hint">{w.author}</div></td>
                      <td>{bytes(w.bytes)}</td>
                      <td style={{ textAlign: "right" }}>
                        {!w.trashed && (
                          <OverflowMenu
                            label={`Clean up ${w.title}`}
                            actions={(["renditions", "covers", "read_aloud"] as const).map((a) => ({
                              label: `${ACTIONS[a].title}…`,
                              onSelect: () => setAsk({ action: a, params: { work_id: w.id }, title: `${ACTIONS[a].title}: ${w.title}` }),
                            }))}
                          />
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </>
      )}

      <h3 style={{ marginTop: 16 }}>Clean up</h3>
      {view.job && <p className="hint" role="status" id="st-job">{jobText(view.job)}</p>}
      <div className="storage-actions">
        <div className="row" style={{ gap: 8 }}>
          <button className="btn sm" disabled={running} onClick={() => setAsk({ action: "trash", params: days ? { older_than_days: days } : {}, title: ACTIONS.trash.title })}>Empty the Trash…</button>
          <span className="row" style={{ gap: 6, alignItems: "center", flexWrap: "nowrap" }}>
            <span className="hint" aria-hidden="true">older than</span>
            <select className="select" style={{ width: "auto" }} value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="Only Trash items older than">
              <option value={0}>any age</option>
              {[1, 7, 14, 21].map((d) => <option key={d} value={d}>{d} {d === 1 ? "day" : "days"}</option>)}
            </select>
          </span>
        </div>
        {(["renditions", "covers", "read_aloud", "leftovers"] as const).map((a) => (
          <button key={a} className="btn sm" disabled={running} onClick={() => setAsk({ action: a, params: {}, title: ACTIONS[a].title })}>{ACTIONS[a].title}…</button>
        ))}
      </div>
      {scan && scan.pre_restore.length > 0 && (
        <>
          <h3 style={{ marginTop: 16 }}>Pre-restore folders</h3>
          <table className="list">
            <tbody>
              {scan.pre_restore.map((p) => {
                const at = preRestoreDate(p.name);
                return (
                  <tr key={p.name}>
                    <td><code>{p.name}</code>{at && <div className="hint">restore of {absoluteTime(at)}</div>}</td>
                    <td>{bytes(p.bytes)}</td>
                    <td style={{ textAlign: "right" }}>
                      <button className="btn sm danger" disabled={running} onClick={() => setAsk({ action: "pre_restore", params: { name: p.name }, title: `Delete ${p.name}` })}>Delete…</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      )}

      <Sheet open={!!ask} onClose={() => setAsk(null)} title={ask?.title ?? ""} variant="dialog" labelledBy="st-confirm-title" wide>
        {ask && (
          <CleanupConfirm
            ask={ask}
            running={running}
            onDone={(v) => {
              setView(v);
              setAsk(null);
            }}
          />
        )}
      </Sheet>
    </section>
  );
}

/** The preview of one cleanup and its explicit confirmation. */
function CleanupConfirm({ ask, running, onDone }: { ask: Ask; running: boolean; onDone: (v: StorageView) => void }) {
  const { toast } = useStore();
  const [preview, setPreview] = useState<Preview | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [keys, setKeys] = useState<Set<string>>(new Set());
  const [ack, setAck] = useState(false);
  const [sending, setSending] = useState(false);
  const fetchPreview = useCallback(() => {
    setFailed(null);
    setAck(false);
    post<Preview>("/api/admin/storage/preview", { action: ask.action, ...ask.params }).then(
      (p) => {
        setPreview(p);
        setKeys(new Set(p.items.map((i) => i.key)));
      },
      (e) => setFailed((e as Error).message),
    );
  }, [ask]);
  useEffect(fetchPreview, [fetchPreview]);
  if (failed) return <div className="alert error" role="alert">{failed}</div>;
  if (!preview) return <div className="hint">Working out what would be removed…</div>;
  const sel = selection(preview.items, keys);
  const toggle = (k: string) => setKeys((s) => {
    const n = new Set(s);
    if (n.has(k)) n.delete(k);
    else n.add(k);
    return n;
  });
  const confirm = async () => {
    setSending(true);
    try {
      const v = await post<StorageView>("/api/admin/storage/cleanup", { action: ask.action, ...ask.params, keys: [...keys], confirm: true });
      toast(`Cleanup started: ${bytes(sel.bytes)} to free.`);
      onDone(v);
    } catch (e) {
      toast((e as Error).message === "preview_changed" ? "Something changed since the preview; here is the new one." : (e as Error).message, "error");
      fetchPreview();
    } finally {
      setSending(false);
    }
  };
  return (
    <div className="storage-confirm">
      <p className="sub">{ACTIONS[ask.action].describe}</p>
      {preview.blocked && <div className="alert warn" role="status">{BLOCKED_TEXT[preview.blocked]}</div>}
      {preview.items.length === 0 ? (
        <p className="hint" role="status">Nothing to remove.</p>
      ) : (
        <>
          <ul className="storage-items" aria-label="What would be removed">
            {preview.items.map((i) => (
              <li key={i.key}>
                <label className="row" style={{ gap: 8, flexWrap: "nowrap" }}>
                  <input type="checkbox" checked={keys.has(i.key)} onChange={() => toggle(i.key)} />
                  <span style={{ flex: 1 }}>{i.label}</span>
                  <span className="hint">{i.files} {i.files === 1 ? "file" : "files"} · {bytes(i.bytes)}</span>
                </label>
              </li>
            ))}
          </ul>
          <p id="st-total"><strong>Frees {bytes(sel.bytes)}</strong> ({sel.files} {sel.files === 1 ? "file" : "files"} in {sel.count} of {preview.items.length} {preview.items.length === 1 ? "item" : "items"}).</p>
        </>
      )}
      {preview.skipped.length > 0 && <p className="hint">Left alone while being built: {preview.skipped.map((s) => s.label).join(", ")}.</p>}
      {preview.items.length > 0 && (
        <>
          <label className="row" style={{ gap: 8, flexWrap: "nowrap", marginTop: 8 }}>
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
            <span>I understand: these files are deleted for good.</span>
          </label>
          <div className="row" style={{ justifyContent: "flex-end", marginTop: 12 }}>
            <button className="btn danger" disabled={!canConfirm({ selected: sel.count, acknowledged: ack, running, sending })} onClick={confirm}>
              {confirmLabel(ask.action, sel)}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
