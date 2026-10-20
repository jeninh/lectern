import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { absoluteTime, bytes, relativeTime } from "../lib/format";
import { useStore } from "../lib/store";
import { Alert, Bell, Check, Close, Upload } from "./Icons";
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

/** Progress for resumable uploads; stays until each item finishes or is dismissed. */
export function UploadTray() {
  const { uploads, uploadManager } = useStore();
  if (!uploads.length || !uploadManager) return null;
  const active = uploads.filter((u) => !["done", "failed", "cancelled"].includes(u.state)).length;
  return (
    <section className="upload-tray" aria-label="Uploads">
      <div className="tray-head">
        <Upload />
        <strong>{active ? `Uploading ${active} ${active === 1 ? "file" : "files"}` : "Uploads"}</strong>
      </div>
      <ul>
        {uploads.map((u) => {
          const pct = u.size ? Math.floor((u.sent / u.size) * 100) : 0;
          const done = u.state === "done" || u.state === "failed" || u.state === "cancelled";
          return (
            <li key={u.key} data-state={u.state}>
              <div className="tray-row">
                <span className="name" title={u.name}>{u.title || u.name}</span>
                <button
                  className="icon-btn ghost sm"
                  aria-label={done ? `Dismiss ${u.name}` : `Cancel upload of ${u.name}`}
                  title={done ? "Dismiss" : "Cancel"}
                  onClick={() => (done ? uploadManager.dismiss(u.key) : uploadManager.cancel(u.key))}
                >
                  <Close />
                </button>
              </div>
              <div className="progress-bar" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label={`${u.name} upload`}>
                <span style={{ width: `${u.state === "done" ? 100 : pct}%` }} />
              </div>
              <div className="hint">
                {u.state === "done"
                  ? "Uploaded. Filing it now."
                  : u.state === "failed"
                    ? u.error || "Upload failed"
                    : u.state === "cancelled"
                      ? "Cancelled"
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
        })}
      </ul>
    </section>
  );
}
