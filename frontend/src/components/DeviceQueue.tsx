import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, del, get, post } from "../lib/api";
import { absoluteTime, relativeTime } from "../lib/format";
import { moveItem } from "../lib/queues";
import { useStore } from "../lib/store";
import type { DeviceQueue, QueueItem, Work } from "../lib/types";
import { Cover } from "./Cover";
import { EmptyState } from "./EmptyState";
import { Book, Chevron, Close, Download } from "./Icons";

/** One device's queue: what is waiting (in catalogue order, reorderable, removable) and what
 * was delivered or removed lately (queue again). */
export function DeviceQueueView({ deviceId, onChanged }: { deviceId: number; onChanged?: () => void }) {
  const { toast, now } = useStore();
  const [q, setQ] = useState<DeviceQueue | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setQ(await get<DeviceQueue>(`/api/device-queues/${deviceId}`));
    } catch (e) {
      setError((e as Error).message);
    }
  }, [deviceId]);
  useEffect(() => {
    load();
  }, [load]);

  const act = async (fn: () => Promise<unknown>, done?: string) => {
    setBusy(true);
    try {
      await fn();
      if (done) toast(done);
      onChanged?.();
    } catch (e) {
      if (e instanceof ApiError && e.code === "queue_changed") toast("The queue changed meanwhile; here is the latest.", "error");
      else toast((e as Error).message, "error");
    } finally {
      await load();
      setBusy(false);
    }
  };

  if (error) return <div className="alert error" role="alert">{error}</div>;
  if (!q) return <div className="stack" style={{ gap: 10 }} aria-busy="true" aria-label="Loading queue">{Array.from({ length: 3 }, (_, i) => <div key={i} className="skeleton-line" />)}</div>;

  const ids = q.pending.map((i) => i.id);
  const move = (index: number, delta: number) => act(() => post(`/api/device-queues/${deviceId}/order`, { item_ids: moveItem(ids, index, delta) }));
  const remove = (item: QueueItem) => act(() => del(`/api/device-queues/${deviceId}/items/${item.id}`), `Removed “${item.work.title}” from ${q.device.label}`);
  const requeue = (item: QueueItem) => act(() => post(`/api/device-queues/${deviceId}/items`, { work_ids: [item.work.id] }), `“${item.work.title}” is waiting on ${q.device.label} again`);

  return (
    <div className="stack" aria-busy={busy || undefined}>
      <p className="hint" style={{ margin: 0 }}>
        {q.device.own ? "Your" : `${q.device.user.display_name || q.device.user.username}’s`} <strong>{q.device.label}</strong> lists these first in its OPDS catalogue, under <em>Queued for this device</em>. A book leaves the queue once this device has downloaded it.
      </p>
      <section aria-labelledby="queue-waiting-h">
        <h3 className="detail-h" id="queue-waiting-h">Waiting · {q.pending.length}</h3>
        {q.pending.length === 0 ? (
          <EmptyState icon={<Book />} title="Nothing waiting" action={<Link className="btn" to="/app/library">Open the library</Link>}>
            Choose <strong>Send to device</strong> on a book, or select several in the library, to queue them here.
          </EmptyState>
        ) : (
          <ol className="queue-list">
            {q.pending.map((item, i) => (
              <li key={item.id} className="queue-item">
                <QueueCover work={item.work} />
                <span className="queue-text">
                  <strong>{item.work.title}</strong>
                  <span className="hint">{item.work.author}</span>
                  <span className="hint" title={absoluteTime(item.queued_at)}>
                    Queued {relativeTime(item.queued_at, now)}{item.queued_by ? ` by ${item.queued_by}` : ""}
                  </span>
                </span>
                <span className="queue-actions">
                  <button className="icon-btn ghost sm" onClick={() => move(i, -1)} disabled={busy || i === 0} aria-label={`Move “${item.work.title}” up`} title="Move up"><Chevron style={{ rotate: "180deg" }} /></button>
                  <button className="icon-btn ghost sm" onClick={() => move(i, 1)} disabled={busy || i === q.pending.length - 1} aria-label={`Move “${item.work.title}” down`} title="Move down"><Chevron /></button>
                  <button className="icon-btn ghost sm" onClick={() => remove(item)} disabled={busy} aria-label={`Remove “${item.work.title}” from the queue`} title="Remove"><Close /></button>
                </span>
              </li>
            ))}
          </ol>
        )}
      </section>
      {q.history.length > 0 && (
        <section aria-labelledby="queue-history-h">
          <h3 className="detail-h" id="queue-history-h">Recently</h3>
          <ul className="queue-list">
            {q.history.map((item) => (
              <li key={item.id} className="queue-item" data-status={item.status}>
                <QueueCover work={item.work} />
                <span className="queue-text">
                  <strong>{item.work.title}</strong>
                  <span className="hint">
                    {item.status === "delivered" ? (
                      <><Download width={12} height={12} /> Downloaded {relativeTime(item.delivered_at, now)}</>
                    ) : (
                      <>Removed {relativeTime(item.removed_at, now)}{item.removed_by ? ` by ${item.removed_by}` : ""}</>
                    )}
                  </span>
                </span>
                <span className="queue-actions">
                  {!q.device.revoked && <button className="btn sm" onClick={() => requeue(item)} disabled={busy}>Queue again</button>}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function QueueCover({ work }: { work: QueueItem["work"] }) {
  return <Cover work={{ ...work, author: work.author } as Work} className="queue-cover" />;
}
