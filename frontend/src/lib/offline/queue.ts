// Positions saved while offline (docs/offline.md#positions).
//
// A device that cannot reach the server keeps the latest position per book here, in the same
// canonical form it would have sent (the body of PUT /api/works/:id/progress, or of
// PUT /api/files/:id/progress for PDFs and comics), with when it was recorded and the server
// time of the last position this device had seen (`base`). On reconnect or focus the queue is
// sent with `offline: {recorded_at, sent_at, base}`. The server applies the rule it uses online,
// last write wins, by recording time instead of arrival time: a position recorded offline loses
// only to one another device saved after it (and after this device last looked). Then the
// server answers 409 and the app asks which to keep.
//
// Pure logic over injected storage and transport, so it is unit-tested.

export type PositionKind = "work" | "page";

export interface ServerPosition {
  /** Work progress: source, device, received_at, percent, audio_s, text, raw. */
  device?: string | null;
  source?: string | null;
  received_at?: string | null;
  percent?: number | null;
  audio_s?: number | null;
  raw?: string | null;
  /** Page progress: page, percentage, updated_at. */
  page?: number | null;
  percentage?: number | null;
  updated_at?: string | null;
}

export interface QueuedPosition {
  key: string;
  kind: PositionKind;
  /** Work id for "work", file id for "page". */
  id: number;
  workId: number;
  title: string;
  body: Record<string, unknown>;
  /** Device clock, ISO 8601. */
  recordedAt: string;
  /** Server time of the last position this device saw for the book, if any. */
  base: string | null;
  /** Set when the server holds a newer position from another device. */
  conflict?: { server: ServerPosition | null; recordedAt: string | null } | null;
}

export interface QueueStorage {
  all(): Promise<QueuedPosition[]>;
  put(e: QueuedPosition): Promise<void>;
  del(key: string): Promise<void>;
}

export type SendResult =
  | { ok: true; seenAt: string | null; server: ServerPosition | null }
  | { ok: false; conflict: true; server: ServerPosition | null; recordedAt: string | null }
  | { ok: false; conflict: false; retry: boolean };

export type Transport = (e: QueuedPosition, opts: { sentAt: string; keep: boolean }) => Promise<SendResult>;

export const keyOf = (kind: PositionKind, id: number) => `${kind}:${id}`;

export class PositionQueue {
  constructor(
    private store: QueueStorage,
    private send: Transport,
    private onSynced: (e: QueuedPosition, seenAt: string | null) => void = () => {},
    private now: () => Date = () => new Date(),
  ) {}

  /** Keep only the newest position per book; a new one replaces an unresolved conflict. */
  async add(e: Omit<QueuedPosition, "key" | "conflict">): Promise<void> {
    await this.store.put({ ...e, key: keyOf(e.kind, e.id), conflict: null });
  }

  async drop(kind: PositionKind, id: number): Promise<void> {
    await this.store.del(keyOf(kind, id));
  }

  list(): Promise<QueuedPosition[]> {
    return this.store.all();
  }

  /** Send every queued position that is not waiting on a decision. Returns what is left. */
  async flush(): Promise<{ synced: number; conflicts: QueuedPosition[]; pending: number }> {
    let synced = 0;
    for (const e of await this.store.all()) {
      if (e.conflict) continue;
      const r = await this.send(e, { sentAt: this.now().toISOString(), keep: false });
      if (r.ok) {
        await this.store.del(e.key);
        this.onSynced(e, r.seenAt);
        synced++;
      } else if (r.conflict) {
        await this.store.put({ ...e, conflict: { server: r.server, recordedAt: r.recordedAt } });
      } else if (!r.retry) {
        // The server will never take it (book gone, position out of range): drop it.
        await this.store.del(e.key);
      } else break; // still offline: try again later, in order
    }
    const left = await this.store.all();
    return { synced, conflicts: left.filter((e) => e.conflict), pending: left.filter((e) => !e.conflict).length };
  }

  /**
   * The person's answer to a conflict. "mine" sends this device's position with `keep`, so it
   * replaces the other device's; "theirs" drops it. Returns the server's position for the app
   * to move to ("theirs"), or null.
   */
  async resolve(key: string, choice: "mine" | "theirs"): Promise<ServerPosition | null> {
    const e = (await this.store.all()).find((x) => x.key === key);
    if (!e) return null;
    if (choice === "theirs") {
      await this.store.del(key);
      const server = e.conflict?.server ?? null;
      this.onSynced(e, server?.received_at ?? server?.updated_at ?? null);
      return server;
    }
    const r = await this.send(e, { sentAt: this.now().toISOString(), keep: true });
    if (r.ok) {
      await this.store.del(key);
      this.onSynced(e, r.seenAt);
    } else if (!r.conflict && !r.retry) await this.store.del(key);
    else if (!r.conflict) throw new Error("offline");
    return null;
  }
}
