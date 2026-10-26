import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { helpPath } from "../lib/help";
import { get, post, put } from "../lib/api";
import { absoluteTime, bytes, relativeTime } from "../lib/format";
import { useVisiblePolling } from "../lib/hooks";
import { useStore } from "../lib/store";
import type { BackupView, OfflineFeature, OfflineModeView } from "../lib/types";

/** "03:00" in the browser's time zone for a UTC hour today (DST and half-hour zones included). */
export function localHourLabel(hourUtc: number, now = new Date()): string {
  const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), hourUtc));
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function Switch({ on, busy, labelledBy, describedBy, onFlip }: { on: boolean; busy: boolean; labelledBy: string; describedBy: string; onFlip: () => void }) {
  return (
    <button type="button" role="switch" className="switch" aria-checked={on} aria-labelledby={labelledBy} aria-describedby={describedBy} onClick={onFlip} disabled={busy}>
      <span aria-hidden="true" />
    </button>
  );
}

export function BackupsCard() {
  const { toast } = useStore();
  const [view, setView] = useState<BackupView | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => get<BackupView>("/api/admin/backups").then(setView), []);
  useEffect(() => {
    load().catch(() => {});
  }, [load]);
  useVisiblePolling(() => {
    if (view?.running) load().catch(() => {});
  }, 2000);
  const save = async (change: Partial<BackupView["config"]>, done?: string) => {
    setBusy(true);
    try {
      setView(await put<BackupView>("/api/admin/backups", change));
      if (done) toast(done);
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  const runNow = async () => {
    try {
      setView(await post<BackupView>("/api/admin/backups/run"));
      toast("Backup started.");
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  if (!view) {
    return (
      <section className="card" id="backups" style={{ marginTop: 18 }}>
        <h2>Backups</h2>
        <div className="hint">Loading…</div>
      </section>
    );
  }
  const { config: c, status: s, target: t } = view;
  const failedLast = s.last_result === "failed";
  return (
    <section className="card" id="backups" aria-labelledby="bk-h" style={{ marginTop: 18 }}>
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "nowrap" }}>
        <div>
          <h2 id="bk-h">Nightly backups</h2>
          <p className="sub" id="bk-sub">
            Copies the database (consistently, while Lectern runs), the signing key, settings, sentence maps, and every book’s metadata and cover, and optionally the library files, into an encrypted restic repository in the backup folder. Off by default.
          </p>
        </div>
        <Switch on={c.enabled} busy={busy} labelledBy="bk-h" describedBy="bk-sub" onFlip={() => save({ enabled: !c.enabled }, c.enabled ? "Nightly backups turned off." : `Nightly backups on, at ${localHourLabel(c.hour_utc)}.`)} />
      </div>

      {!t.writable ? (
        <div className="alert error" role="alert" style={{ marginBottom: 12 }}>
          The backup folder <code>{t.path}</code> {t.exists ? "is not writable by Lectern" : "does not exist"}. Set <code>LECTERN_BACKUP_PATH</code> in <code>.env</code> to a folder owned by Lectern’s user (for example on a USB drive) and restart.
        </div>
      ) : t.same_disk_as_data ? (
        <div className="alert warn" style={{ marginBottom: 12 }}>
          The backup folder is on the same disk as Lectern’s data, so a failing disk takes both. On a Raspberry Pi that is usually the SD card, which also wears out faster: point <code>LECTERN_BACKUP_PATH</code> at a USB drive.
        </div>
      ) : null}
      {failedLast && s.last_error && (
        <div className="alert error" role="alert" style={{ marginBottom: 12 }}>
          <strong>The last backup failed</strong> {relativeTime(s.last_error_at)}: {s.last_error}
        </div>
      )}
      {!failedLast && !!s.last_skipped_files && (
        <div className="alert warn" style={{ marginBottom: 12 }}>
          The last backup left out {s.last_skipped_files} unreadable file{s.last_skipped_files === 1 ? "" : "s"}{s.last_skipped_examples?.length ? <>, e.g. <code>{s.last_skipped_examples[0]}</code></> : null}. Check their permissions.
        </div>
      )}
      {!t.restic_available && (
        <div className="alert error" role="alert" style={{ marginBottom: 12 }}>
          restic is not installed on the server. It ships in the Docker image; rebuild it.
        </div>
      )}

      <dl className="kv" style={{ marginBottom: 14 }}>
        <dt>Last success</dt>
        <dd>
          {s.last_success_at ? (
            <>
              <time title={absoluteTime(s.last_success_at)}>{relativeTime(s.last_success_at)}</time>
              {s.last_location && <> · <code>{s.last_location}</code></>}
              {s.last_size_bytes != null && <> · {bytes(s.last_size_bytes)}</>}
            </>
          ) : (
            <span className="hint">never</span>
          )}
        </dd>
        <dt>Status</dt>
        <dd>
          {view.running ? <span className="badge accent">running…</span> : failedLast ? <span className="badge danger">failed</span> : s.last_result === "ok" ? <span className="badge ok">ok</span> : <span className="badge">{c.enabled ? "waiting for the first night" : "off"}</span>}
        </dd>
        <dt>Folder</dt>
        <dd>
          <code>{t.path}</code>
          {t.free_bytes != null && <span className="hint"> · {bytes(t.free_bytes)} free</span>}
        </dd>
      </dl>

      <div className="settings-grid">
        <label className="field">
          <span>Nightly at (your time)</span>
          <select className="select" value={c.hour_utc} disabled={busy} onChange={(e) => save({ hour_utc: Number(e.target.value) })}>
            {Array.from({ length: 24 }, (_, h) => (
              <option key={h} value={h}>{localHourLabel(h)}</option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Keep the newest</span>
          <select className="select" value={c.retention} disabled={busy} onChange={(e) => save({ retention: Number(e.target.value) })}>
            {Array.from(new Set([3, 7, 14, 30, 60, 90, c.retention])).sort((a, b) => a - b).map((n) => (
              <option key={n} value={n}>{n} backups</option>
            ))}
          </select>
        </label>
      </div>
      <label className="field settings-check">
        <span>
          <input type="checkbox" checked={c.include_library} disabled={busy} onChange={(e) => save({ include_library: e.target.checked })} />
          Include library files (books and audiobooks)
        </span>
      </label>
      <p className="hint">
        Backups go into an encrypted restic repository in the backup folder; after the first night only changes are stored. Its password is in <code>{t.restic_password_file}</code>: save a copy in your password manager, because without it the backups cannot be read. Restoring is a command on the host with Lectern stopped; see <Link to={helpPath("restore")}>Restore</Link>.
      </p>
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button type="button" className="btn" onClick={runNow} disabled={view.running || !t.writable || !t.restic_available}>{view.running ? "Backing up…" : "Back up now"}</button>
      </div>
    </section>
  );
}

/** Offline mode (docs/offline-mode.md): one switch that refuses every outbound internet
 * connection, what would otherwise contact the internet, and downloading models ahead of time. */
export function OfflineModeCard() {
  const { toast, refreshMe } = useStore();
  const [view, setView] = useState<OfflineModeView | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => get<OfflineModeView>("/api/admin/offline-mode").then(setView), []);
  useEffect(() => {
    load().catch(() => {});
  }, [load]);
  useVisiblePolling(() => {
    if (view?.prepare.running) load().catch(() => {});
  }, 2000);
  const flip = async () => {
    if (!view) return;
    setBusy(true);
    try {
      setView(await put<OfflineModeView>("/api/admin/offline-mode", { enabled: !view.on }));
      await refreshMe();
      toast(view.on ? "Offline mode turned off." : "Offline mode on: Lectern makes no internet connections.");
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  const prepare = async () => {
    try {
      setView(await post<OfflineModeView>("/api/admin/offline-mode/prepare"));
      toast("Downloading models…");
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  if (!view) {
    return (
      <section className="card" id="offline-mode" style={{ marginTop: 18 }}>
        <h2>Offline mode</h2>
        <div className="hint">Loading…</div>
      </section>
    );
  }
  const { models: m, prepare: p } = view;
  const status = (f: OfflineFeature) =>
    f.blocked ? <span className="badge warn">blocked</span> : f.on ? <span className="badge accent">on</span> : <span className="badge">off</span>;
  return (
    <section className="card" id="offline-mode" aria-labelledby="off-h" style={{ marginTop: 18 }}>
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "nowrap" }}>
        <div>
          <h2 id="off-h">Offline mode</h2>
          <p className="sub" id="off-sub">
            Guarantees that this server never contacts the internet: every outbound connection is refused, whatever asks for it. Readers, devices, and workers still reach Lectern as before. For a guarantee that also covers ffmpeg and the operating system, add the firewall rules in <code>docs/offline-mode.md</code>.
          </p>
        </div>
        <Switch on={view.on} busy={busy || view.forced} labelledBy="off-h" describedBy="off-sub" onFlip={flip} />
      </div>
      {view.forced && (
        <div className="alert ok" style={{ marginBottom: 12 }}>
          <code>LECTERN_OFFLINE=1</code> is set on the server, so offline mode stays on.
        </div>
      )}
      <table className="list" style={{ marginBottom: 14 }}>
        <thead>
          <tr><th scope="col">What contacts the internet</th><th scope="col">Where</th><th scope="col">Now</th></tr>
        </thead>
        <tbody>
          {view.features.map((f) => (
            <tr key={f.id}>
              <td><strong>{f.name}</strong><div className="hint">{f.when}</div></td>
              <td>{f.contacts.map((h) => <div key={h}><code>{h}</code></div>)}</td>
              <td>{status(f)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <dl className="kv" style={{ marginBottom: 14 }}>
        <dt>Alignment model</dt>
        <dd>{!m.align.local ? <span className="hint">this server does not align</span> : m.align.ready ? <><span className="badge ok">downloaded</span> {m.align.model}</> : <><span className="badge warn">not downloaded</span> {m.align.model}</>}</dd>
        <dt>Kokoro voices</dt>
        <dd>{!m.kokoro.local ? <span className="hint">not run on this server</span> : m.kokoro.ready ? <span className="badge ok">downloaded</span> : <span className="badge warn">not downloaded</span>}</dd>
        <dt>Piper voices</dt>
        <dd>{!m.piper.installed ? <span className="hint">Piper is not installed</span> : m.piper.voices.length ? m.piper.voices.join(", ") : <span className="badge warn">none</span>}</dd>
      </dl>
      {p.finished_at && !p.running && (
        <div className={`alert ${p.ok ? "ok" : "error"}`} role={p.ok ? undefined : "alert"} style={{ marginBottom: 12 }}>
          {p.ok ? "Models downloaded" : "The download failed"} {relativeTime(p.finished_at)}.
          {!p.ok && p.log.length > 0 && <> <code>{p.log[p.log.length - 1]}</code></>}
        </div>
      )}
      {p.running && p.log.length > 0 && <p className="hint" aria-live="polite">{p.log[p.log.length - 1]}</p>}
      <p className="hint">
        Download what this server needs to align and narrate without the internet before you turn offline mode on (the same as <code>lectern prepare-models</code>). Models go into the data directory.
      </p>
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button type="button" className="btn" onClick={prepare} disabled={view.on || p.running} title={view.on ? "Turn offline mode off to download" : undefined}>{p.running ? "Downloading…" : "Download models now"}</button>
      </div>
    </section>
  );
}
