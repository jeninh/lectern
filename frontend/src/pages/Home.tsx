import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ContinueCard } from "../components/ContinueCard";
import { Cover } from "../components/Cover";
import { EmptyState } from "../components/EmptyState";
import { Book, Devices as DevicesIcon, Plus } from "../components/Icons";
import { PageTitle } from "../components/Shell";
import { get } from "../lib/api";
import { useVisiblePolling } from "../lib/hooks";
import { useStore } from "../lib/store";
import type { SyncKey, Work } from "../lib/types";
import { ActivityFeed } from "./Activity";

export function HomePage() {
  const { me, works, worksLoaded } = useStore();
  const [items, setItems] = useState<Work[] | null>(null);
  const [devices, setDevices] = useState<SyncKey[] | null>(null);

  const load = useCallback(async () => {
    const [c, d] = await Promise.all([get<{ items: Work[] }>("/api/continue"), get<{ sync_keys: SyncKey[] }>("/api/devices")]);
    setItems(c.items);
    setDevices(d.sync_keys.filter((k) => !k.revoked_at));
  }, []);
  useEffect(() => {
    load().catch(() => {});
  }, [load]);
  useVisiblePolling(() => load().catch(() => {}), 15000);

  const hour = new Date().getHours();
  const greet = hour < 5 ? "Still up" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const name = me?.user.display_name || me?.user.username || "";
  const hasDevices = (devices?.length ?? 0) > 0;

  return (
    <>
      <PageTitle>{`${greet}, ${name}`}</PageTitle>
      <div className="home-grid with-side">
        <div className="stack">
          <section aria-labelledby="continue-h">
            <div className="section-head">
              <h2 id="continue-h">Continue reading</h2>
              {items && items.length > 0 && <span className="badge"><span className="live-dot" aria-hidden="true" /> live</span>}
              <Link className="more" to="/app/library">All books</Link>
            </div>
            {items === null ? (
              <div className="hint">Loading…</div>
            ) : items.length === 0 ? (
              worksLoaded && works.length === 0 ? (
                <EmptyState icon={<Book />} title="Your library is empty">
                  Drop EPUB files into <code>{me?.inbox_path ?? "the inbox"}</code>. They are matched, filed, and show up here within seconds.
                </EmptyState>
              ) : !hasDevices ? (
                <EmptyState
                  icon={<DevicesIcon />}
                  title="Nothing synced yet"
                  action={<Link className="btn primary" to="/app/devices/new"><Plus /> Add a device</Link>}
                >
                  Connect KOReader and your reading positions will appear here, tinted by each book's cover.
                </EmptyState>
              ) : (
                <EmptyState icon={<Book />} title="Waiting for your first sync">
                  Open a book in KOReader on a connected device. The position it reports lands here.
                </EmptyState>
              )
            ) : (
              <div className="continue-grid">
                {items.map((w) => (
                  <ContinueCard key={w.id} work={w} />
                ))}
              </div>
            )}
          </section>

          {worksLoaded && works.length > 0 && (
            <section aria-labelledby="recent-h">
              <div className="section-head">
                <h2 id="recent-h">Recently added</h2>
                <Link className="more" to="/app/library?sort=added">Library</Link>
              </div>
              <div className="book-grid">
                {[...works]
                  .sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""))
                  .slice(0, 6)
                  .map((w) => (
                    <Link key={w.id} className="book" to={`/app/library/${w.id}`} style={{ textDecoration: "none" }}>
                      <Cover work={w} />
                      <strong>{w.title}</strong>
                      <span>{w.author}</span>
                    </Link>
                  ))}
              </div>
            </section>
          )}
        </div>

        <aside className="stack" aria-labelledby="activity-h">
          <div className="section-head">
            <h2 id="activity-h">Recent activity</h2>
            <Link className="more" to="/app/activity">All</Link>
          </div>
          <div className="card" style={{ padding: "6px 10px" }}>
            <ActivityFeed limit={8} compact />
          </div>
          {devices && (
            <div className="card">
              <h2 style={{ fontSize: 17 }}>Devices</h2>
              <p className="sub">{devices.length === 0 ? "No devices connected." : `${devices.length} active sync key${devices.length === 1 ? "" : "s"}.`}</p>
              <Link className="btn sm" to="/app/devices/new"><Plus /> Add a device</Link>
            </div>
          )}
        </aside>
      </div>
    </>
  );
}
