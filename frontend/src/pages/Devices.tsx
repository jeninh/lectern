import { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { AIToolsSection } from "../components/AITools";
import { EmptyState } from "../components/EmptyState";
import { DeviceQueueView } from "../components/DeviceQueue";
import { KOReaderPlugin } from "../components/KOReaderPlugin";
import { Book, Close, Download, Plus, Reader, Send, Shelf as ShelfIcon, Sync } from "../components/Icons";
import { OverflowMenu } from "../components/Menu";
import { Sheet } from "../components/Sheet";
import { PageTitle } from "../components/Shell";
import { get, post } from "../lib/api";
import { absoluteTime, relativeTime } from "../lib/format";
import { useVisiblePolling } from "../lib/hooks";
import { pluginSetupText } from "../lib/koplugin";
import { followOptions, shelfSyncChips } from "../lib/queues";
import { useStore } from "../lib/store";
import type { AppPassword, Device, QueueDevice, Shelf } from "../lib/types";
import { AddDeviceFlow } from "./AddDevice";

const ACTIVE_MS = 5 * 60 * 1000;

export function deviceStatus(d: Device, now = Date.now()): { tone: "live" | "ok" | "waiting" | "off"; text: string } {
  if (d.revoked_at) return { tone: "off", text: "Revoked" };
  if (!d.last_used_at) return { tone: "waiting", text: "Waiting for first sync" };
  if (now - new Date(d.last_used_at).getTime() < ACTIVE_MS) return { tone: "live", text: "Active now" };
  return { tone: "ok", text: "Connected" };
}

export function DevicesPage() {
  const { me, toast, now, shelves } = useStore();
  const nav = useNavigate();
  const loc = useLocation();
  const adding = loc.pathname.endsWith("/new");
  const [devices, setDevices] = useState<Device[] | null>(null);
  const [confirm, setConfirm] = useState<Device | null>(null);
  const [capFor, setCapFor] = useState<Device | null>(null);
  const [syncFor, setSyncFor] = useState<Device | null>(null);
  const [showRevoked, setShowRevoked] = useState(false);
  const [queues, setQueues] = useState<QueueDevice[]>([]);
  const [queueFor, setQueueFor] = useState<QueueDevice | null>(null);
  const [pluginFor, setPluginFor] = useState<string | null>(null);
  const pluginDevice = (devices ?? []).find((d) => d.id === pluginFor && !d.revoked_at) ?? null;

  const load = useCallback(async () => {
    const [d, q] = await Promise.all([get<{ devices: Device[] }>("/api/devices"), get<{ devices: QueueDevice[] }>("/api/device-queues")]);
    setDevices(d.devices);
    setQueues(q.devices);
  }, []);
  const queueOf = (d: Device) => (d.app_password ? queues.find((q) => q.id === d.app_password!.id && q.own) : undefined);
  const household = queues.filter((q) => !q.own);
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
                <DeviceCard key={d.id} d={d} now={now} shelves={shelves} highlight={target === d.id} queue={queueOf(d)} onQueue={(q) => setQueueFor(q)} onRevoke={() => setConfirm(d)} onCap={() => setCapFor(d)} onShelfSync={() => setSyncFor(d)} onPlugin={d.koplugin ? () => setPluginFor(d.id) : undefined} />
              ))}
            </ul>
          )}
          {household.length > 0 && (
            <section aria-labelledby="household-h">
              <div className="section-head"><h2 id="household-h">Household devices</h2></div>
              <p className="hint" style={{ marginTop: 0 }}>As the owner you can send books to anyone’s e-reader and tidy their queues. They only receive books they can already see.</p>
              <ul className="queue-devices">
                {household.map((q) => (
                  <li key={q.id}>
                    <span className="device-icon" aria-hidden="true"><Reader /></span>
                    <span className="queue-text">
                      <strong>{q.label}</strong>
                      <span className="hint">{q.user.display_name || q.user.username} · {q.pending ? `${q.pending} waiting` : "queue empty"} · catalogue {relativeTime(q.last_used_at, now)}</span>
                    </span>
                    <button className="btn sm" onClick={() => setQueueFor(q)}>Open queue</button>
                  </li>
                ))}
              </ul>
            </section>
          )}
          {revoked.length > 0 && (
            <section aria-labelledby="revoked-h">
              <button className="disclosure" aria-expanded={showRevoked} aria-controls="revoked-list" onClick={() => setShowRevoked((v) => !v)} id="revoked-h">
                Revoked ({revoked.length})
              </button>
              {showRevoked && (
                <ul className="device-grid" id="revoked-list">
                  {revoked.map((d) => (
                    <DeviceCard key={d.id} d={d} now={now} shelves={shelves} />
                  ))}
                </ul>
              )}
            </section>
          )}
        </div>
      )}

      <div style={{ marginTop: 28 }}>
        <AIToolsSection />
      </div>

      <Sheet open={adding} onClose={() => nav("/app/devices")} title="Add a device" labelledBy="add-device-title">
        <AddDeviceFlow onDone={() => nav("/app/devices")} onCreated={() => load().catch(() => {})} />
      </Sheet>

      <Sheet open={!!queueFor} onClose={() => setQueueFor(null)} title={queueFor ? `Queue for ${queueFor.label}` : ""} labelledBy="queue-title">
        {queueFor && <DeviceQueueView deviceId={queueFor.id} onChanged={() => load().catch(() => {})} />}
      </Sheet>

      <Sheet open={!!pluginDevice} onClose={() => setPluginFor(null)} title={pluginDevice ? `KOReader plugin for ${pluginDevice.label}` : ""} labelledBy="plugin-title">
        {pluginDevice && <KOReaderPlugin device={pluginDevice} onChanged={() => load().catch(() => {})} />}
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
      <Sheet open={!!syncFor} onClose={() => setSyncFor(null)} title={syncFor ? `Shelf sync for ${syncFor.label}` : ""} variant="dialog" labelledBy="shelf-sync-title">
        {syncFor?.app_password && (
          <ShelfSyncForm
            initial={syncFor.app_password}
            shelves={shelves}
            plugin={!!syncFor.koplugin}
            onSave={async (body) => {
              await post(`/api/devices/app-passwords/${syncFor.app_password!.id}/shelf-sync`, body);
              toast(`Shelf sync for ${syncFor.label} saved`);
              setSyncFor(null);
              await load();
            }}
          />
        )}
      </Sheet>
      <Sheet open={!!capFor} onClose={() => setCapFor(null)} title={capFor ? `Download limit for ${capFor.label}` : ""} variant="dialog" labelledBy="cap-title">
        {capFor?.app_password && (
          <CapForm
            initial={capFor.app_password.download_cap_per_hour ?? null}
            what="this device"
            lowerOnly={me?.user.role !== "owner"}
            onSave={async (cap) => {
              await post(`/api/devices/app-passwords/${capFor.app_password!.id}/download-cap`, { cap });
              toast(`Limit for ${capFor.label} saved`);
              setCapFor(null);
              await load();
            }}
          />
        )}
      </Sheet>
    </>
  );
}

/** Hourly download cap: server default, a number, or no limit. Shared with Admin. */
export function CapForm({ initial, what, onSave, lowerOnly = false }: { initial: number | null; what: string; onSave: (cap: number | null) => Promise<void>; lowerOnly?: boolean }) {
  const [mode, setMode] = useState<"default" | "custom" | "none">(initial === null ? "default" : initial === 0 ? "none" : "custom");
  const [n, setN] = useState(initial && initial > 0 ? initial : 30);
  const [err, setErr] = useState<string | null>(null);
  return (
    <form
      onSubmit={async (e) => {
        e.preventDefault();
        try {
          await onSave(mode === "default" ? null : mode === "none" ? 0 : n);
        } catch (ex) {
          setErr((ex as Error).message);
        }
      }}
    >
      {err && <div className="alert error" role="alert" style={{ marginBottom: 12 }}>{err}</div>}
      <fieldset className="radio-list">
        <legend className="sr-only">Hourly download limit for {what}</legend>
        <label><input type="radio" name="cap" checked={mode === "default"} onChange={() => setMode("default")} /> Server default</label>
        <label>
          <input type="radio" name="cap" checked={mode === "custom"} onChange={() => setMode("custom")} /> At most
          <input className="input sm" type="number" min={1} max={100000} value={n} onChange={(e) => { setN(Number(e.target.value)); setMode("custom"); }} aria-label="Downloads per hour" style={{ width: 90 }} />
          per hour
        </label>
        {!lowerOnly && <label><input type="radio" name="cap" checked={mode === "none"} onChange={() => setMode("none")} /> No limit</label>}
      </fieldset>
      <p className="hint">Downloads over the limit are refused with a clear message and recorded in Activity.{lowerOnly && " You can lower this device's limit; only the owner can raise it."}</p>
      <div className="row" style={{ justifyContent: "flex-end" }}><button className="btn primary">Save</button></div>
    </form>
  );
}

type ShelfSync = { shelf_id: number | null; cleanup: boolean; auto_download: boolean };

/** Shelf sync: the shelf a device follows, clean-up, and the plugin's automatic downloads. */
export function ShelfSyncForm({ initial, shelves, plugin, onSave }: { initial: AppPassword; shelves: readonly Shelf[]; plugin: boolean; onSave: (body: ShelfSync) => Promise<void> }) {
  const [shelfId, setShelfId] = useState<number | null>(initial.follow_shelf_id ?? null);
  const [cleanup, setCleanup] = useState(!!initial.follow_cleanup);
  const [auto, setAuto] = useState(!!initial.auto_download);
  const [err, setErr] = useState<string | null>(null);
  const options = followOptions(shelves);
  const hidden = shelfId !== null && !options.some((o) => o.id === shelfId);
  return (
    <form
      onSubmit={async (e) => {
        e.preventDefault();
        try {
          await onSave({ shelf_id: shelfId, cleanup: shelfId !== null && cleanup, auto_download: auto });
        } catch (ex) {
          setErr((ex as Error).message);
        }
      }}
    >
      {err && <div className="alert error" role="alert" style={{ marginBottom: 12 }}>{err}</div>}
      <label className="field">
        <span>Follow a shelf</span>
        <select className="input" value={shelfId ?? ""} onChange={(e) => setShelfId(e.target.value ? Number(e.target.value) : null)}>
          <option value="">No shelf</option>
          {hidden && <option value={shelfId!}>A shelf you can’t see</option>}
          {options.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
        </select>
      </label>
      <p className="hint">Books on the shelf, and on shelves inside it, are queued for this device by themselves, a few at a time; for a smart shelf, whatever its filter matches at the next sync.</p>
      <fieldset className="radio-list">
        <legend className="sr-only">Options</legend>
        <label><input type="checkbox" checked={shelfId !== null && cleanup} disabled={shelfId === null} onChange={(e) => setCleanup(e.target.checked)} /> Remove books that leave the shelf</label>
        <label><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> Download queued books automatically</label>
      </fieldset>
      <p className="hint">
        {plugin ? "The KOReader plugin downloads queued books when it syncs on its own, within this device’s download limit, and deletes only copies it downloaded itself. Other catalogue apps just see the queue change." : "Without the KOReader plugin, removing only takes books off this device’s queue, and nothing downloads by itself."}
      </p>
      <div className="row" style={{ justifyContent: "flex-end" }}><button className="btn primary">Save</button></div>
    </form>
  );
}

function DeviceCard({ d, now, shelves, highlight, queue, onQueue, onRevoke, onCap, onShelfSync, onPlugin }: { d: Device; now: number; shelves: readonly Shelf[]; highlight?: boolean; queue?: QueueDevice; onQueue?: (q: QueueDevice) => void; onRevoke?: () => void; onCap?: () => void; onShelfSync?: () => void; onPlugin?: () => void }) {
  const s = deviceStatus(d, now);
  const plugin = d.revoked_at ? null : pluginSetupText(d.plugin, now);
  const sync = d.revoked_at ? [] : shelfSyncChips(d.app_password, shelves);
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
        {onRevoke && (
          <OverflowMenu
            label={`Actions for ${d.label}`}
            actions={[
              ...(onPlugin ? [{ label: "KOReader plugin…", icon: <Download />, onSelect: onPlugin }] : []),
              ...(queue && onQueue ? [{ label: "Queue…", icon: <Send />, onSelect: () => onQueue(queue) }] : []),
              ...(d.app_password && onShelfSync ? [{ label: "Shelf sync…", icon: <ShelfIcon />, onSelect: onShelfSync }] : []),
              ...(d.app_password && onCap ? [{ label: "Download limit…", icon: <Download />, onSelect: onCap }] : []),
              { label: "Revoke device", icon: <Close />, danger: true, onSelect: onRevoke },
            ]}
          />
        )}
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
        {plugin && <span className={`cap plugin-${plugin.tone}`}><Reader /> {plugin.text}</span>}
        {sync.map((c) => <span key={c.text} className="cap" title={c.title}>{c.text.startsWith("Follows") ? <ShelfIcon /> : <Download />} {c.text}</span>)}
        {d.app_password?.download_cap_per_hour != null && <span className="cap"><Download /> {d.app_password.download_cap_per_hour ? `${d.app_password.download_cap_per_hour}/h` : "no limit"}</span>}
      </div>
      {queue && onQueue && (
        <button className="queue-summary" onClick={() => onQueue(queue)} aria-label={`Queue for ${d.label}: ${queue.pending ? `${queue.pending} waiting` : "empty"}. Open queue`}>
          <Send aria-hidden="true" />
          <span>{queue.pending ? <><strong>{queue.pending}</strong> {queue.pending === 1 ? "book" : "books"} waiting to download</> : "Nothing queued"}</span>
          <span className="go" aria-hidden="true">Open queue</span>
        </button>
      )}
      {onPlugin && !d.plugin && (
        <button className="btn sm ghost plugin-cta" onClick={onPlugin}><Download /> Set up with the KOReader plugin</button>
      )}
    </li>
  );
}
