import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { absoluteTime, bytes, relativeTime } from "../lib/format";
import { useStore } from "../lib/store";
import { Alert, Bell, Check, Close, Down, Pause, Play, Retry, Upload } from "./Icons";
import { isTerminal, type UploadItem } from "../lib/uploads";
import { Sheet } from "./Sheet";

/** Topbar bell with the unread count; opens the notification sheet. */
export function NotificationBell() {
  const { unread, refreshNotifications } = useStore();
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        className="icon-btn bell"
        onClick={() => {
          setOpen(true);
          refreshNotifications().catch(() => {});
        }}
        aria-label={unread ? `Notifications, ${unread} unread` : "Notifications"}
        title="Notifications"
      >
        <Bell />
        {unread > 0 && <span className="count" aria-hidden="true">{unread > 99 ? "99+" : unread}</span>}
      </button>
      <Sheet open={open} onClose={() => setOpen(false)} title="Notifications" labelledBy="notif-title">
        <NotificationList onNavigate={() => setOpen(false)} />
      </Sheet>
    </>
  );
}

const LINK: Record<string, { to: string; label: string }> = {
  "device.new": { to: "/app/security#sessions", label: "Review sessions" },
  "login.new": { to: "/app/security#sessions", label: "Sessions" },
  "device.connected": { to: "/app/devices", label: "Devices" },
  "download.burst": { to: "/app/security#downloads", label: "Download limits" },
  "download.cap": { to: "/app/security#downloads", label: "Download limits" },
  "invite.accepted": { to: "/app/admin#invites", label: "Household" },
  "health.alert": { to: "/app/health", label: "Health" },
  "backup.failed": { to: "/app/health", label: "Health" },
};

export function NotificationList({ onNavigate }: { onNavigate?: () => void }) {
  const { notifications, unread, markAllRead, now } = useStore();
  useEffect(() => {
    // Opening the list reads it; give the eye a moment on the highlights first.
    if (!unread) return;
    const id = window.setTimeout(() => markAllRead().catch(() => {}), 1500);
    return () => window.clearTimeout(id);
  }, [unread, markAllRead]);
  if (!notifications.length) {
    return <p className="hint">Nothing yet. New sign-ins, new devices, and unusual download activity appear here.</p>;
  }
  return (
    <div className="stack" style={{ gap: 4 }}>
      {unread > 0 && (
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button className="btn sm ghost" onClick={() => markAllRead()}><Check /> Mark all read</button>
        </div>
      )}
      <ul className="notif-list">
        {notifications.map((n) => {
          const link = LINK[n.kind];
          return (
            <li key={n.id} className={`notif ${n.severity}`} data-unread={!n.read_at || undefined}>
              <span className="notif-icon" aria-hidden="true">{n.severity === "warning" ? <Alert /> : <Bell />}</span>
              <div>
                <strong>{n.title}</strong>
                {n.body && <p>{n.body}</p>}
                <div className="hint">
                  <time dateTime={n.created_at ?? undefined} title={absoluteTime(n.created_at)}>{relativeTime(n.created_at, now)}</time>
                  {!n.read_at && <span className="sr-only"> · unread</span>}
                  {link && <> · <Link to={link.to} onClick={onNavigate}>{link.label}</Link></>}
                </div>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** Per-file progress for resumable uploads, grouped by book, with pause, resume, and retry. */
export function UploadTray() {
  const { uploads, uploadManager, imports, setReviewOpen } = useStore();
  const [collapsed, setCollapsed] = useState(false);
  if (!uploadManager || (!uploads.length && !imports.some((i) => i.status === "ready"))) return null;
  const active = uploads.filter((u) => !isTerminal(u.state)).length;
  const sent = uploads.filter((u) => !isTerminal(u.state)).reduce((a, u) => a + u.sent, 0);
  const total = uploads.filter((u) => !isTerminal(u.state)).reduce((a, u) => a + u.size, 0);
  const ready = imports.filter((i) => i.status === "ready").length;
  const groups = new Map<string, UploadItem[]>();
  for (const u of uploads) groups.set(u.group ?? u.key, [...(groups.get(u.group ?? u.key) ?? []), u]);
  return (
    <section className="upload-tray" aria-label="Adding books" data-collapsed={collapsed || undefined}>
      <div className="tray-head">
        <Upload />
        <strong>{active ? `Uploading ${active} ${active === 1 ? "file" : "files"}${total ? ` · ${Math.floor((sent / total) * 100)}%` : ""}` : "Adding books"}</strong>
        <button className="icon-btn ghost sm" onClick={() => setCollapsed(!collapsed)} aria-expanded={!collapsed} aria-label={collapsed ? "Show uploads" : "Hide upload details"} title={collapsed ? "Show" : "Hide"}>
          <Down style={{ transform: collapsed ? "rotate(180deg)" : undefined, transition: "transform var(--dur-spring) var(--spring)" }} />
        </button>
      </div>
      {ready > 0 && (
        <button className="btn sm primary tray-review" onClick={() => setReviewOpen(true)}>
          Review {ready} {ready === 1 ? "book" : "books"} before filing
        </button>
      )}
      {!collapsed && (
        <ul>
          {[...groups.entries()].map(([gid, items]) => {
            const label = uploadManager.groupLabel(items[0].group);
            return (
              <li key={gid} className="tray-group">
                {label && items.length > 1 && <div className="tray-book" title={label}>{label} <span className="hint">· {items.length} files</span></div>}
                <ul>
                  {items.map((u) => <TrayItem key={u.key} u={u} />)}
                </ul>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function TrayItem({ u }: { u: UploadItem }) {
  const { uploadManager } = useStore();
  if (!uploadManager) return null;
  const pct = u.size ? Math.floor((u.sent / u.size) * 100) : 0;
  const done = isTerminal(u.state);
  const canPause = u.state === "uploading" || u.state === "queued" || u.state === "retrying";
  return (
    <li data-state={u.state}>
      <div className="tray-row">
        <span className="name" title={u.name}>{u.group ? u.name : u.title || u.name}</span>
        {canPause && (
          <button className="icon-btn ghost sm" aria-label={`Pause ${u.name}`} title="Pause" onClick={() => uploadManager.pause(u.key)}>
            <Pause />
          </button>
        )}
        {u.state === "paused" && (
          <button className="icon-btn ghost sm" aria-label={`Resume ${u.name}`} title="Resume" onClick={() => uploadManager.resume(u.key)}>
            <Play />
          </button>
        )}
        {(u.state === "failed" || u.state === "cancelled") && u.size <= uploadManager.maxBytes && (
          <button className="icon-btn ghost sm" aria-label={`Retry ${u.name}`} title="Retry" onClick={() => uploadManager.retry(u.key)}>
            <Retry />
          </button>
        )}
        <button
          className="icon-btn ghost sm"
          aria-label={done ? `Dismiss ${u.name}` : `Cancel upload of ${u.name}`}
          title={done ? "Dismiss" : "Cancel"}
          onClick={() => (done ? uploadManager.dismiss(u.key) : uploadManager.cancel(u.key))}
        >
          <Close />
        </button>
      </div>
      <div className="progress-bar" role="progressbar" aria-valuenow={u.state === "done" ? 100 : pct} aria-valuemin={0} aria-valuemax={100} aria-label={`${u.name} upload`}>
        <span style={{ width: `${u.state === "done" ? 100 : pct}%` }} />
      </div>
      <div className="hint">
        {u.state === "done"
          ? u.duplicateOf
            ? "Already in your library, so it is skipped."
            : u.group
              ? "Uploaded."
              : "Uploaded. Filing it now."
          : u.state === "failed"
            ? u.error || "Upload failed"
            : u.state === "cancelled"
              ? "Cancelled"
              : u.state === "paused"
                ? `Paused at ${bytes(u.sent)} of ${bytes(u.size)}`
                : u.state === "finalizing"
                  ? "Checking the file…"
                  : u.state === "retrying"
                    ? `Connection lost. Resuming from ${bytes(u.sent)}…`
                    : u.state === "queued"
                      ? `Waiting · ${bytes(u.size)}`
                      : `${bytes(u.sent)} of ${bytes(u.size)}`}
      </div>
    </li>
  );
}
