import { useEffect, useId, useState } from "react";
import { Link } from "react-router-dom";
import { get, post } from "../lib/api";
import { relativeTime } from "../lib/format";
import { useLocalStorage } from "../lib/hooks";
import { groupDevices, summarizeQueueResults } from "../lib/queues";
import { useStore } from "../lib/store";
import type { QueueDevice, QueueResult } from "../lib/types";
import type { BulkTarget } from "./BulkBar";
import { EmptyState } from "./EmptyState";
import { Plus, Reader } from "./Icons";
import { Sheet } from "./Sheet";

/** "Send to device": pick one OPDS device (yours, or anyone's for the owner) and queue the
 * works for it. They appear first in that device's catalogue under "Queued for this device". */
export function SendToDevice({ open, workIds, target, count, title, onClose, onSent }: { open: boolean; workIds?: number[]; target?: () => BulkTarget; count?: number; title: string; onClose: () => void; onSent?: () => void }) {
  return (
    <Sheet open={open} onClose={onClose} title={title} variant="dialog" labelledBy="send-title">
      {open && <SendForm body={target ? target() : { work_ids: workIds ?? [] }} count={count ?? workIds?.length ?? 0} onClose={onClose} onSent={onSent} />}
    </Sheet>
  );
}

function SendForm({ body, count, onClose, onSent }: { body: BulkTarget; count: number; onClose: () => void; onSent?: () => void }) {
  const { toast, now } = useStore();
  const [devices, setDevices] = useState<QueueDevice[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [last, setLast] = useLocalStorage<number | null>("lectern.sendDevice", null);
  const [choice, setChoice] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const single = "work_ids" in body && body.work_ids.length === 1 ? body.work_ids[0] : null;
  const hintId = useId();

  useEffect(() => {
    get<{ devices: QueueDevice[] }>(`/api/device-queues${single ? `?work_id=${single}` : ""}`)
      .then((d) => {
        setDevices(d.devices);
        const remembered = d.devices.find((x) => x.id === last);
        setChoice((remembered ?? d.devices.find((x) => x.own) ?? d.devices[0])?.id ?? null);
      })
      .catch((e) => setError((e as Error).message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [single]);

  if (error) return <div className="alert error" role="alert">{error}</div>;
  if (devices === null) return <div className="stack" style={{ gap: 10 }} aria-busy="true" aria-label="Loading devices">{Array.from({ length: 3 }, (_, i) => <div key={i} className="skeleton-line" />)}</div>;
  if (devices.length === 0)
    return (
      <EmptyState icon={<Reader />} title="No e-reader to send to" action={<Link className="btn primary" to="/app/devices/new" onClick={onClose}><Plus /> Add a device</Link>}>
        Books are sent through a device’s OPDS catalogue. Add your Kobo, Kindle with KOReader, or reading app first.
      </EmptyState>
    );

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const device = devices.find((d) => d.id === choice);
    if (!device) return;
    setBusy(true);
    try {
      const r = await post<{ results: QueueResult[] }>(`/api/device-queues/${device.id}/items`, body);
      const s = summarizeQueueResults(r.results, device);
      toast(s.text, s.kind);
      setLast(device.id);
      onSent?.();
      onClose();
    } catch (ex) {
      setError((ex as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="stack" style={{ gap: 14 }}>
      {groupDevices(devices).map((g) => (
        <fieldset key={g.label} className="send-list" aria-describedby={hintId}>
          <legend>{g.label}</legend>
          {g.devices.map((d) => (
            <label key={d.id} className="send-option" data-checked={choice === d.id || undefined}>
              <input type="radio" name="device" value={d.id} checked={choice === d.id} onChange={() => setChoice(d.id)} />
              <span className="device-icon" aria-hidden="true"><Reader /></span>
              <span className="send-text">
                <strong>{d.label}</strong>
                <span className="hint">
                  {d.queued ? "Already waiting there" : d.pending ? `${d.pending} waiting` : "Queue empty"} · {d.last_used_at ? `catalogue opened ${relativeTime(d.last_used_at, now)}` : "catalogue never opened"}
                </span>
              </span>
              {d.queued && <span className="badge accent">Queued</span>}
            </label>
          ))}
        </fieldset>
      ))}
      <p className="hint" id={hintId} style={{ margin: 0 }}>
        It shows up first in that device’s catalogue under <em>Queued for this device</em>, and leaves the queue once that device has downloaded it.
      </p>
      <div className="sheet-actions">
        <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={busy || choice === null} data-autofocus>
          {count === 1 ? "Send" : `Send ${count} books`}
        </button>
      </div>
    </form>
  );
}
