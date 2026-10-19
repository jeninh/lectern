import { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { EmptyState } from "../components/EmptyState";
import { Book, Close, Plus, Reader, Sync } from "../components/Icons";
import { OverflowMenu } from "../components/Menu";
import { Sheet } from "../components/Sheet";
import { PageTitle } from "../components/Shell";
import { get, post } from "../lib/api";
import { absoluteTime, relativeTime } from "../lib/format";
import { useVisiblePolling } from "../lib/hooks";
import { useStore } from "../lib/store";
import type { Device } from "../lib/types";
import { AddDeviceFlow } from "./AddDevice";

const ACTIVE_MS = 5 * 60 * 1000;

export function deviceStatus(d: Device, now = Date.now()): { tone: "live" | "ok" | "waiting" | "off"; text: string } {
  if (d.revoked_at) return { tone: "off", text: "Revoked" };
  if (!d.last_used_at) return { tone: "waiting", text: "Waiting for first sync" };
  if (now - new Date(d.last_used_at).getTime() < ACTIVE_MS) return { tone: "live", text: "Active now" };
  return { tone: "ok", text: "Connected" };
}

export function DevicesPage() {
  const { toast, now } = useStore();
  const nav = useNavigate();
  const loc = useLocation();
  const adding = loc.pathname.endsWith("/new");
  const [devices, setDevices] = useState<Device[] | null>(null);
  const [confirm, setConfirm] = useState<Device | null>(null);
  const [showRevoked, setShowRevoked] = useState(false);

  const load = useCallback(async () => {
    const d = await get<{ devices: Device[] }>("/api/devices");
    setDevices(d.devices);
  }, []);
  useEffect(() => {
    load().catch(() => {});
  }, [load]);
  useVisiblePolling(() => load().catch(() => {}), adding ? 4000 : 15000);

  // Deep link from the palette: /app/devices#k12 scrolls to and briefly highlights that card.
  const target = loc.hash.slice(1);
  useEffect(() => {
    if (!target || !devices) return;
    document.getElementById(`device-${target}`)?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [target, devices]);

  const revoke = async () => {
    if (!confirm) return;
    try {
      await post(`/api/devices/${confirm.id}/revoke`);
      toast(`Revoked “${confirm.label}”`);
      setConfirm(null);
      await load();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  const active = (devices ?? []).filter((d) => !d.revoked_at);
  const revoked = (devices ?? []).filter((d) => d.revoked_at);
  const liveCount = active.filter((d) => deviceStatus(d, now).tone === "live").length;
  const openAdd = () => nav("/app/devices/new");

  return (
    <>
      <PageTitle
        sub={devices ? `${active.length} active${liveCount ? ` · ${liveCount} syncing now` : ""}` : undefined}
        aside={<button className="btn primary" onClick={openAdd} aria-keyshortcuts="N"><Plus /> Add device <kbd className="kbd-inline" aria-hidden="true">N</kbd></button>}
      >
        Devices
      </PageTitle>

      {devices === null ? (
        <div className="device-grid" aria-busy="true" aria-label="Loading"><div className="device-card skeleton" /><div className="device-card skeleton" /></div>
      ) : active.length === 0 && revoked.length === 0 ? (
        <EmptyState icon={<Reader />} title="No devices yet" action={<button className="btn primary" onClick={openAdd}><Plus /> Add your first device</button>}>
          Each e-reader or phone gets its own sync key and catalogue password, so you can revoke one without touching the others.
        </EmptyState>
      ) : (
        <div className="stack">
          {active.length === 0 ? (
            <EmptyState icon={<Reader />} title="No active devices" action={<button className="btn primary" onClick={openAdd}><Plus /> Add a device</button>}>
              Every device has been revoked. Add one to start syncing again.
            </EmptyState>
          ) : (
            <ul className="device-grid" aria-label="Active devices">
              {active.map((d) => (
                <DeviceCard key={d.id} d={d} now={now} highlight={target === d.id} onRevoke={() => setConfirm(d)} />
              ))}
            </ul>
          )}
          {revoked.length > 0 && (
            <section aria-labelledby="revoked-h">
              <button className="disclosure" aria-expanded={showRevoked} aria-controls="revoked-list" onClick={() => setShowRevoked((v) => !v)} id="revoked-h">
                Revoked ({revoked.length})
              </button>
              {showRevoked && (
                <ul className="device-grid" id="revoked-list">
                  {revoked.map((d) => (
                    <DeviceCard key={d.id} d={d} now={now} />
                  ))}
                </ul>
              )}
            </section>
          )}
        </div>
      )}

      <Sheet open={adding} onClose={() => nav("/app/devices")} title="Add a device" labelledBy="add-device-title">
        <AddDeviceFlow onDone={() => nav("/app/devices")} onCreated={() => load().catch(() => {})} />
      </Sheet>

      <Sheet open={!!confirm} onClose={() => setConfirm(null)} title={`Revoke “${confirm?.label}”?`} variant="dialog" labelledBy="revoke-title">
        <p style={{ marginTop: 0 }}>
          It stops syncing and loses catalogue access immediately. Saved reading positions stay on the server, and you can add the device again later.
        </p>
        <div className="sheet-actions">
          <button className="btn ghost" onClick={() => setConfirm(null)}>Keep device</button>
          <button className="btn danger" onClick={revoke} data-autofocus>Revoke</button>
        </div>
      </Sheet>
    </>
  );
}

function DeviceCard({ d, now, highlight, onRevoke }: { d: Device; now: number; highlight?: boolean; onRevoke?: () => void }) {
  const s = deviceStatus(d, now);
  return (
    <li className="device-card" id={`device-${d.id}`} data-tone={s.tone} data-highlight={highlight || undefined}>
      <div className="device-head">
        <span className="device-icon" aria-hidden="true"><Reader /></span>
        <div className="device-name">
          <h3>{d.label}</h3>
          <span className={`status ${s.tone}`}>
            <span className={`status-dot ${s.tone}`} aria-hidden="true" />
            {s.text}
          </span>
        </div>
        {onRevoke && <OverflowMenu label={`Actions for ${d.label}`} actions={[{ label: "Revoke device", icon: <Close />, danger: true, onSelect: onRevoke }]} />}
      </div>
      <dl className="device-meta">
        <div>
          <dt>Last sync</dt>
          <dd title={absoluteTime(d.sync_key?.last_used_at)}>{d.sync_key ? relativeTime(d.sync_key.last_used_at, now) : "—"}</dd>
        </div>
        <div>
          <dt>Catalogue</dt>
          <dd title={absoluteTime(d.app_password?.last_used_at)}>{d.app_password ? relativeTime(d.app_password.last_used_at, now) : "—"}</dd>
        </div>
        <div>
          <dt>{d.revoked_at ? "Revoked" : "Added"}</dt>
          <dd title={absoluteTime(d.revoked_at ?? d.created_at)}>{relativeTime(d.revoked_at ?? d.created_at, now)}</dd>
        </div>
      </dl>
      <div className="caps" aria-label="Capabilities">
        {d.sync_key && <span className="cap"><Sync /> Progress sync</span>}
        {d.app_password && <span className="cap"><Book /> Catalogue</span>}
      </div>
    </li>
  );
}
