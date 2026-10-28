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
  /** The work's listening speed (issue #71). */
  speed?: number | null;
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

/** `keep`: the person chose this device's position over `expect`, the server time of the
 * position their prompt showed. The server takes it only if it still holds that one. */
export type Transport = (e: QueuedPosition, opts: { sentAt: string; keep: boolean; expect?: string | null }) => Promise<SendResult>;

/** The server time of a position, as the conflict prompt showed it. */
export const serverTime = (p: ServerPosition | null | undefined): string | null => p?.received_at ?? p?.updated_at ?? null;

export const keyOf = (kind: PositionKind, id: number) => `${kind}:${id}`;

/**
 * Every read-modify-write of the queue runs one at a time (`locked`), and what a send's answer
 * does to an entry is compare-and-set: it applies only if the entry is still the one that was
 * sent (same `recordedAt`). Sends themselves run outside the lock, so a save made while one is
 * in flight is never held up by the network, and never undone by the answer arriving after it:
 * an entry dropped by an online save does not come back as a conflict, and a newer position
 * queued meanwhile is not deleted when the older one syncs.
 */
export class PositionQueue {
  private tail: Promise<unknown> = Promise.resolve();

  constructor(
    private store: QueueStorage,
    private send: Transport,
    private onSynced: (e: QueuedPosition, seenAt: string | null) => void = () => {},
    private now: () => Date = () => new Date(),
  ) {}

  private locked<T>(fn: () => Promise<T>): Promise<T> {
    const run = this.tail.then(fn, fn);
    this.tail = run.catch(() => {});
    return run;
  }

  private async current(key: string): Promise<QueuedPosition | undefined> {
    return (await this.store.all()).find((x) => x.key === key);
  }

  /** Apply `fn` to the entry if it is still the one sent; returns whether it was. */
  private settle(sent: QueuedPosition, fn: (cur: QueuedPosition) => Promise<void>): Promise<boolean> {
    return this.locked(async () => {
      const cur = await this.current(sent.key);
      if (!cur || cur.recordedAt !== sent.recordedAt) return false;
      await fn(cur);
      return true;
    });
  }

  /** Keep only the newest position per book; a new one replaces an unresolved conflict. */
  add(e: Omit<QueuedPosition, "key" | "conflict">): Promise<void> {
    return this.locked(() => this.store.put({ ...e, key: keyOf(e.kind, e.id), conflict: null }));
  }

  /** Forget what is queued for a book because a save made at `savedAt` (device clock) reached
   * the server. A position recorded after it stays. */
  drop(kind: PositionKind, id: number, savedAt?: string): Promise<void> {
    return this.locked(async () => {
      const cur = await this.current(keyOf(kind, id));
      if (cur && (savedAt == null || cur.recordedAt <= savedAt)) await this.store.del(cur.key);
    });
  }

  list(): Promise<QueuedPosition[]> {
    return this.locked(() => this.store.all());
  }

  /** Send every queued position that is not waiting on a decision. Returns what is left. */
  async flush(): Promise<{ synced: number; conflicts: QueuedPosition[]; pending: number }> {
    let synced = 0;
    for (const e of await this.list()) {
      if (e.conflict) continue;
      const r = await this.send(e, { sentAt: this.now().toISOString(), keep: false });
      if (r.ok) {
        if (await this.settle(e, (cur) => this.store.del(cur.key))) {
          this.onSynced(e, r.seenAt);
          synced++;
        }
      } else if (r.conflict) {
        await this.settle(e, (cur) => this.store.put({ ...cur, conflict: { server: r.server, recordedAt: r.recordedAt } }));
      } else if (!r.retry) {
        // The server will never take it (book gone, position out of range): drop it.
        await this.settle(e, (cur) => this.store.del(cur.key));
      } else break; // still offline: try again later, in order
    }
    const left = await this.list();
    return { synced, conflicts: left.filter((e) => e.conflict), pending: left.filter((e) => !e.conflict).length };
  }

  /**
   * The person's answer to a conflict. "mine" sends this device's position with `keep` and the
   * server time the prompt showed: if another device saved again since, the conflict is updated
   * with that newer position and the prompt asks again. "theirs" drops it. Returns the server's
   * position for the app to move to ("theirs"), or null.
   */
  async resolve(key: string, choice: "mine" | "theirs"): Promise<ServerPosition | null> {
    const e = (await this.list()).find((x) => x.key === key);
    if (!e?.conflict) return null;
    if (choice === "theirs") {
      if (!(await this.settle(e, (cur) => this.store.del(cur.key)))) return null;
      const server = e.conflict.server ?? null;
      this.onSynced(e, serverTime(server));
      return server;
    }
    const r = await this.send(e, { sentAt: this.now().toISOString(), keep: true, expect: serverTime(e.conflict.server) });
    if (r.ok) {
      if (await this.settle(e, (cur) => this.store.del(cur.key))) this.onSynced(e, r.seenAt);
    } else if (r.conflict) {
      await this.settle(e, (cur) => this.store.put({ ...cur, conflict: { server: r.server, recordedAt: r.recordedAt } }));
    } else if (!r.retry) await this.settle(e, (cur) => this.store.del(cur.key));
    else throw new Error("offline");
    return null;
  }
}
