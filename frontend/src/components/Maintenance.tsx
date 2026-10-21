import { useCallback, useEffect, useState } from "react";
import { get, post, put } from "../lib/api";
import { absoluteTime, bytes, relativeTime } from "../lib/format";
import { useVisiblePolling } from "../lib/hooks";
import { useStore } from "../lib/store";
import type { BackupView } from "../lib/types";

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
        Backups go into an encrypted restic repository in the backup folder; after the first night only changes are stored. Its password is in <code>{t.restic_password_file}</code>: save a copy in your password manager, because without it the backups cannot be read. Restoring is a command on the host with Lectern stopped; see <code>docs/backups.md</code>.
      </p>
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button type="button" className="btn" onClick={runNow} disabled={view.running || !t.writable || !t.restic_available}>{view.running ? "Backing up…" : "Back up now"}</button>
      </div>
    </section>
  );
}
