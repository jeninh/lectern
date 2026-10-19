import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ContinueCard } from "../components/ContinueCard";
import { Cover, tintStyle } from "../components/Cover";
import { EmptyState } from "../components/EmptyState";
import { Book, Devices as DevicesIcon, Plus, Reader, Upload } from "../components/Icons";
import { PageTitle } from "../components/Shell";
import { get } from "../lib/api";
import { relativeTime } from "../lib/format";
import { useVisiblePolling } from "../lib/hooks";
import { useStore } from "../lib/store";
import type { Device, Work } from "../lib/types";
import { ActivityFeed } from "./Activity";
import { deviceStatus } from "./Devices";

export function HomePage() {
  const { me, works, worksLoaded, canUpload, pickBooks, now } = useStore();
  const [items, setItems] = useState<Work[] | null>(null);
  const [devices, setDevices] = useState<Device[] | null>(null);

  const load = useCallback(async () => {
    const [c, d] = await Promise.all([get<{ items: Work[] }>("/api/continue"), get<{ devices: Device[] }>("/api/devices")]);
    setItems(c.items);
    setDevices(d.devices.filter((x) => !x.revoked_at));
  }, []);
  useEffect(() => {
    load().catch(() => {});
  }, [load]);
  useVisiblePolling(() => load().catch(() => {}), 15000);

  const hour = new Date(now).getHours();
  const greet = hour < 5 ? "Still up" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const name = me?.user.display_name || me?.user.username || "";
  const hasDevices = (devices?.length ?? 0) > 0;
  const reading = items?.length ?? 0;
  const sub = worksLoaded
    ? `${works.length} ${works.length === 1 ? "book" : "books"} · ${reading} in progress · ${devices?.length ?? 0} ${devices?.length === 1 ? "device" : "devices"}`
    : undefined;

  const uploadAction = canUpload ? (
    <button className="btn primary" onClick={pickBooks}><Upload /> Upload EPUBs</button>
  ) : undefined;

  return (
    <>
      <PageTitle sub={sub}>{`${greet}, ${name}`}</PageTitle>
      <div className="home-grid with-side">
        <div className="stack">
          <section aria-labelledby="continue-h">
            <div className="section-head">
              <h2 id="continue-h">Continue reading</h2>
              {reading > 0 && <span className="badge live"><span className="live-dot" aria-hidden="true" /> Live</span>}
              <Link className="more" to="/app/library">All books →</Link>
            </div>
            {items === null ? (
              <div className="skeleton-row" aria-busy="true" aria-label="Loading"><span /><span /></div>
            ) : items.length === 0 ? (
              worksLoaded && works.length === 0 ? (
                <EmptyState icon={<Book />} title="Your shelves are empty" action={uploadAction}>
                  Upload EPUBs here or drop them into <code>{me?.inbox_path ?? "the inbox"}</code>. Lectern reads their metadata, files them by author, and they show up within seconds.
                </EmptyState>
              ) : !hasDevices ? (
                <EmptyState
                  icon={<DevicesIcon />}
                  title="Nothing synced yet"
                  action={<Link className="btn primary" to="/app/devices/new"><Plus /> Add a device</Link>}
                >
                  Connect KOReader and your reading positions appear here, tinted by each book’s cover.
                </EmptyState>
              ) : (
                <EmptyState icon={<Reader />} title="Waiting for your first sync" action={<Link className="btn" to="/app/devices">Check devices</Link>}>
                  Open any book in KOReader on a connected device and turn a page. Its position lands here on its own.
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
                <Link className="more" to="/app/library?sort=added">Library →</Link>
              </div>
              <div className="shelf">
                {[...works]
                  .sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""))
                  .slice(0, 8)
                  .map((w) => (
                    <Link key={w.id} className="book" to={`/app/library/${w.id}`} style={tintStyle(w)}>
                      <Cover work={w} />
                      <strong>{w.title}</strong>
                      <span>{w.author}</span>
                    </Link>
                  ))}
              </div>
            </section>
          )}
        </div>

        <aside className="stack side" aria-label="Activity and devices">
          <section aria-labelledby="activity-h">
            <div className="section-head">
              <h2 id="activity-h">Recent activity</h2>
              <Link className="more" to="/app/activity">All →</Link>
            </div>
            <div className="card flush">
              <ActivityFeed limit={7} compact />
            </div>
          </section>
          {devices && (
            <section className="card" aria-labelledby="devices-h">
              <div className="section-head" style={{ marginBottom: 8 }}>
                <h2 id="devices-h" style={{ fontSize: 18 }}>Devices</h2>
                <Link className="more" to="/app/devices">Manage →</Link>
              </div>
              {devices.length === 0 ? (
                <p className="hint" style={{ margin: "0 0 12px" }}>No devices yet. Each one gets its own revocable credentials.</p>
              ) : (
                <ul className="mini-devices">
                  {devices.slice(0, 4).map((d) => {
                    const s = deviceStatus(d, now);
                    return (
                      <li key={d.id}>
                        <span className={`status-dot ${s.tone}`} aria-hidden="true" />
                        <strong>{d.label}</strong>
                        <span className="hint">{d.last_used_at ? relativeTime(d.last_used_at, now) : s.text}</span>
                      </li>
                    );
                  })}
                </ul>
              )}
              <Link className="btn sm ghost" to="/app/devices/new"><Plus /> Add device</Link>
            </section>
          )}
        </aside>
      </div>
    </>
  );
}
