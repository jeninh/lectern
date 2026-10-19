import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { EmptyState } from "../components/EmptyState";
import { Alert, Devices, Download, Key, Login, Logout, Pulse, Shield, Sync, Upload, Users } from "../components/Icons";
import { PageTitle } from "../components/Shell";
import { get } from "../lib/api";
import { absoluteTime, percent, relativeTime } from "../lib/format";
import { useVisiblePolling } from "../lib/hooks";
import { useStore } from "../lib/store";
import type { ActivityEvent } from "../lib/types";

function describe(e: ActivityEvent): { icon: JSX.Element; cls: string; text: JSX.Element } {
  const who = e.actor ? <strong>{e.actor}</strong> : <strong>someone</strong>;
  switch (e.action) {
    case "sync.progress":
      return {
        icon: <Sync />,
        cls: "sync",
        text: (
          <>
            {who} reached <strong>{percent(e.percentage)}</strong> in {e.work_id ? <Link to={`/app/library/${e.work_id}`}>{e.target}</Link> : e.target}
            {e.device && <span> on {e.device}</span>}
          </>
        ),
      };
    case "login.success":
      return { icon: <Login />, cls: "ok", text: <>{who} signed in <span>({e.detail})</span></> };
    case "login.failed":
      return { icon: <Alert />, cls: "warn", text: <>Failed sign-in for <strong>{e.target}</strong> <span>({e.detail})</span></> };
    case "login.locked_out":
      return { icon: <Alert />, cls: "warn", text: <><strong>{e.target}</strong> locked out after repeated failures</> };
    case "logout":
      return { icon: <Logout />, cls: "", text: <>{who} signed out</> };
    case "download":
      return { icon: <Download />, cls: "", text: <>{who} downloaded a book <span>via {e.detail}</span></> };
    case "sync_key.create":
      return { icon: <Key />, cls: "ok", text: <>{who} added sync key <strong>{e.target}</strong></> };
    case "sync_key.revoke":
      return { icon: <Key />, cls: "warn", text: <>{who} revoked sync key <strong>{e.target}</strong></> };
    case "app_password.create":
      return { icon: <Key />, cls: "ok", text: <>{who} added app password <strong>{e.target}</strong></> };
    case "app_password.revoke":
      return { icon: <Key />, cls: "warn", text: <>{who} revoked app password <strong>{e.target}</strong></> };
    case "passkey.register":
      return { icon: <Shield />, cls: "ok", text: <>{who} added passkey <strong>{e.target}</strong></> };
    case "passkey.delete":
      return { icon: <Shield />, cls: "warn", text: <>{who} removed passkey <strong>{e.target}</strong></> };
    case "totp.enable":
      return { icon: <Shield />, cls: "ok", text: <>{who} enabled two-factor codes</> };
    case "totp.disable":
      return { icon: <Shield />, cls: "warn", text: <>{who} disabled two-factor codes</> };
    case "password.change":
      return { icon: <Shield />, cls: "ok", text: <>{who} changed their password</> };
    case "session.revoke":
      return { icon: <Shield />, cls: "", text: <>{who} signed out {e.target === "others" ? `${e.detail} other session(s)` : "a session"}</> };
    case "user.create":
      return { icon: <Users />, cls: "ok", text: <>{who} created user <strong>{e.target}</strong> <span>({e.detail})</span></> };
    case "user.disable":
      return { icon: <Users />, cls: "warn", text: <>{who} disabled <strong>{e.target}</strong></> };
    case "user.enable":
      return { icon: <Users />, cls: "ok", text: <>{who} re-enabled <strong>{e.target}</strong></> };
    case "device.create":
      return { icon: <Devices />, cls: "ok", text: <>{who} added device <strong>{e.target}</strong></> };
    case "device.revoke":
      return { icon: <Devices />, cls: "warn", text: <>{who} revoked device <strong>{e.target}</strong></> };
    case "book.upload":
      return { icon: <Upload />, cls: "", text: <>{who} uploaded <strong>{e.target}</strong></> };
    case "setup.owner_created":
      return { icon: <Users />, cls: "ok", text: <>Owner account <strong>{e.target}</strong> created</> };
    default:
      return { icon: <Pulse />, cls: "", text: <>{e.action}</> };
  }
}

export function ActivityFeed({ limit = 50, compact = false }: { limit?: number; compact?: boolean }) {
  const [events, setEvents] = useState<ActivityEvent[] | null>(null);
  const since = useRef<string | null>(null);
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  const { now } = useStore();

  const load = useCallback(async () => {
    if (since.current) {
      const d = await get<{ events: ActivityEvent[]; now: string }>(`/api/activity?since=${encodeURIComponent(since.current)}&limit=${limit}`);
      since.current = d.now;
      if (d.events.length) {
        setFresh(new Set(d.events.map((e) => e.id)));
        window.setTimeout(() => setFresh(new Set()), 4000);
        setEvents((prev) => [...d.events, ...(prev ?? []).filter((p) => !d.events.some((n) => n.id === p.id))].slice(0, limit));
      }
    } else {
      const d = await get<{ events: ActivityEvent[]; now: string }>(`/api/activity?limit=${limit}`);
      since.current = d.now;
      setEvents(d.events);
    }
  }, [limit]);
  useEffect(() => {
    load().catch(() => {});
  }, [load]);
  useVisiblePolling(() => load().catch(() => {}), 10000);

  if (events === null) return <div className="hint" style={{ padding: 10 }}>Loading…</div>;
  if (events.length === 0)
    return (
      <EmptyState icon={<Pulse />} title="Quiet so far">
        Sign-ins, syncs, downloads, and credential changes will show up here as they happen.
      </EmptyState>
    );
  return (
    <div className="timeline" role="feed" aria-busy="false">
      {events.map((e) => {
        const d = describe(e);
        return (
          <article className="event" key={e.id} aria-label={typeof e.action === "string" ? e.action : undefined} data-fresh={fresh.has(e.id) || undefined}>
            <span className={`ico ${d.cls}`} aria-hidden="true">{d.icon}</span>
            <div className="what">
              {d.text}
              {!compact && e.ip && <span> · {e.ip}</span>}
            </div>
            <time dateTime={e.at ?? undefined} title={absoluteTime(e.at)}>{relativeTime(e.at, now)}</time>
          </article>
        );
      })}
    </div>
  );
}

export function ActivityPage() {
  return (
    <>
      <PageTitle aside={<span className="badge"><span className="live-dot" aria-hidden="true" /> updates every 10 s</span>}>Activity</PageTitle>
      <div className="card" style={{ padding: "6px 12px" }}>
        <ActivityFeed limit={100} />
      </div>
    </>
  );
}
