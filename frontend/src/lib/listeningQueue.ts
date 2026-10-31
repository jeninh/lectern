// The listening queue is cacheable only on this device, and its edits are durable until the
// server acknowledges them. The server version is a compare-and-set guard; replay never blindly
// writes an old list over someone else's newer edit.

import { ApiError, get, OfflineError, post } from "./api";
import { idb } from "./offline/db";
import type { ListeningQueue } from "./types";

type Operation =
  | { operation: "enqueue"; work_id: number; track_id: number | null; chapter_start_s?: number | null; chapter_end_s?: number | null }
  | { operation: "remove"; item_id: number }
  | { operation: "clear"; item_ids: number[] }
  | { operation: "reorder"; item_ids: number[] };
type Mutation = Operation & { id: string; base_version: number; sequence?: number };

const STATE = "state";
const SEQUENCE = "sequence";
const changed = () => window.dispatchEvent(new Event("lectern:listening-queue"));
const key = () => crypto.randomUUID();

async function cached(): Promise<ListeningQueue> {
  return (await idb.get<ListeningQueue>("listening-queue", STATE)) ?? { version: 0, items: [] };
}
async function remember(queue: ListeningQueue) {
  await idb.put("listening-queue", STATE, queue);
  changed();
  return queue;
}
function optimistic(queue: ListeningQueue, op: Operation): ListeningQueue {
  const items = op.operation === "remove" ? queue.items.filter((x) => x.id !== op.item_id)
    : op.operation === "clear" ? queue.items.filter((x) => !op.item_ids.includes(x.id))
      : op.operation === "reorder" ? op.item_ids.flatMap((id) => queue.items.filter((x) => x.id === id)) : queue.items;
  return { version: queue.version + 1, items };
}
async function send(m: Mutation): Promise<ListeningQueue> {
  const { sequence: _sequence, ...body } = m;
  return post<ListeningQueue>("/api/listening-queue/mutations", body, { "idempotency-key": m.id });
}
function isMutation(value: unknown): value is Mutation {
  return typeof value === "object" && value != null && "id" in value && "operation" in value;
}
function pending(rows: unknown[]): Mutation[] {
  // Older installs have UUID-keyed records without a sequence. They still replay before newly
  // created records, and their UUID tie-breaker makes that upgrade path deterministic.
  return rows.filter(isMutation).sort((a, b) =>
    (a.sequence ?? -1) - (b.sequence ?? -1) || a.id.localeCompare(b.id),
  );
}

/** The current server queue, falling back to the per-user device cache while offline. */
export async function listeningQueue(): Promise<ListeningQueue> {
  try {
    return await remember(await get<ListeningQueue>("/api/listening-queue"));
  } catch (e) {
    if (e instanceof OfflineError) return cached();
    throw e;
  }
}

async function rebase(m: Mutation): Promise<ListeningQueue> {
  const fresh = await get<ListeningQueue>("/api/listening-queue");
  // Reordering is presentation-only: preserve the other device's order rather than overwriting
  // it. Clears remove only items the person saw, so later enqueues survive a conflict.
  if (m.operation === "reorder") return remember(fresh);
  if (m.operation === "remove" && !fresh.items.some((x) => x.id === m.item_id)) return remember(fresh);
  const next = { ...m, base_version: fresh.version };
  return remember(await send(next));
}

async function run(op: Operation): Promise<ListeningQueue> {
  const before = await cached();
  const m: Mutation = { ...op, id: key(), base_version: before.version };
  try {
    return await remember(await send(m));
  } catch (e) {
    if (e instanceof ApiError && e.code === "queue_changed") return rebase(m);
    if (!(e instanceof OfflineError)) throw e;
    const queued = { ...m, sequence: await idb.next("listening-queue", SEQUENCE) };
    await idb.put("listening-queue", m.id, queued);
    return remember(optimistic(before, op));
  }
}

/** Replay edits one at a time. A network failure leaves the exact mutation and key intact. */
export async function flushListeningQueue(): Promise<void> {
  for (const m of pending(await idb.all("listening-queue"))) {
    try {
      await remember(await send(m));
      await idb.del("listening-queue", m.id);
    } catch (e) {
      if (e instanceof ApiError && e.code === "queue_changed") {
        await rebase(m);
        await idb.del("listening-queue", m.id);
      } else if (e instanceof ApiError && e.status === 404 && m.operation === "remove") {
        await idb.del("listening-queue", m.id); // another device already consumed it
      } else {
        break;
      }
    }
  }
}

export const enqueueListening = (work_id: number, track_id: number | null = null, chapter_start_s: number | null = null, chapter_end_s: number | null = null) =>
  run({ operation: "enqueue", work_id, track_id, chapter_start_s, chapter_end_s });
export const removeListening = (item_id: number) => run({ operation: "remove", item_id });
export const consumeListening = removeListening;
export const clearListening = async () => {
  const queue = await cached();
  return run({ operation: "clear", item_ids: queue.items.map((x) => x.id) });
};
export const reorderListening = (item_ids: number[]) => run({ operation: "reorder", item_ids });
