// Resumable uploads (docs/contracts.md "Resumable uploads"): create a session, send ≤ 50 MiB
// chunks at the server's offset with a per-chunk SHA-256, resume from the server's offset after
// any failure, and finalize with the whole-file SHA-256. Survives flaky home uplinks and, with
// the same file picked again, a page reload.

import { ApiError, del, get, post, putChunk } from "./api";
import type { UploadSessionInfo } from "./types";

export type UploadState = "queued" | "uploading" | "retrying" | "finalizing" | "done" | "failed" | "cancelled";

export interface UploadItem {
  key: string;
  name: string;
  size: number;
  sent: number;
  state: UploadState;
  error?: string;
  title?: string | null;
  sessionId?: string;
}

/** What the manager needs from the server; the real one wraps fetch, tests use a fake. */
export interface Transport {
  create(file: { name: string; size: number; type: string }, idempotencyKey: string): Promise<UploadSessionInfo>;
  status(id: string): Promise<UploadSessionInfo>;
  chunk(id: string, offset: number, digest: string, body: Blob, signal: AbortSignal): Promise<number>;
  finalize(id: string, sha256: string): Promise<{ title?: string | null; queued?: string }>;
  cancel(id: string): Promise<void>;
}

export interface Hasher {
  chunk(key: string, file: Blob, start: number, end: number): Promise<string>;
  finish(key: string, file: Blob): Promise<string>;
  drop(key: string): void;
}

export interface Store {
  get(key: string): { id: string; idem: string } | undefined;
  set(key: string, v: { id: string; idem: string }): void;
  delete(key: string): void;
}

const MEDIA: Record<string, string> = {
  epub: "application/epub+zip",
  mp3: "audio/mpeg",
  m4b: "audio/mp4",
  m4a: "audio/mp4",
  mp4: "audio/mp4",
  ogg: "audio/ogg",
  oga: "audio/ogg",
  opus: "audio/ogg",
  flac: "audio/flac",
};
export const ACCEPT = ".epub,.m4b,.m4a,.mp3,.ogg,.opus,.oga,.flac,application/epub+zip,audio/mpeg,audio/mp4,audio/ogg,audio/flac";

export function mediaTypeOf(name: string): string | null {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  return MEDIA[ext] ?? null;
}

export function fileKey(f: { name: string; size: number; lastModified?: number }): string {
  return `${f.name}:${f.size}:${f.lastModified ?? 0}`;
}

/** Chunk boundaries from ``from`` to the end of the file. */
export function planChunks(size: number, chunkSize: number, from = 0): Array<[number, number]> {
  const out: Array<[number, number]> = [];
  for (let start = from; start < size; start += chunkSize) out.push([start, Math.min(size, start + chunkSize)]);
  return out;
}

const RETRYABLE = new Set([408, 425, 429, 500, 502, 503, 504]);
export function isRetryable(err: unknown): boolean {
  if (err instanceof ApiError) return RETRYABLE.has(err.status) && err.code !== "download_cap" && err.code !== "too_many_uploads";
  return err instanceof TypeError || (err as Error)?.name === "NetworkError"; // fetch: network down
}

export function backoff(attempt: number): number {
  return Math.min(30_000, 1000 * 2 ** attempt);
}

type Listener = (items: UploadItem[]) => void;

export class UploadManager {
  items: UploadItem[] = [];
  private files = new Map<string, File | Blob>();
  private aborts = new Map<string, AbortController>();
  private listeners = new Set<Listener>();
  private running = false;
  onDone?: (item: UploadItem) => void;

  constructor(
    private transport: Transport,
    private hasher: Hasher,
    private store: Store,
    private sleep: (ms: number) => Promise<void> = (ms) => new Promise((r) => setTimeout(r, ms)),
    private maxAttempts = 8,
  ) {}

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    fn(this.items);
    return () => this.listeners.delete(fn);
  }

  private emit() {
    this.items = this.items.map((i) => ({ ...i }));
    for (const fn of this.listeners) fn(this.items);
  }

  private update(key: string, patch: Partial<UploadItem>) {
    const it = this.items.find((i) => i.key === key);
    if (it) Object.assign(it, patch);
    this.emit();
  }

  /** Queue files. Unsupported types are reported and skipped. Returns the rejected names. */
  add(files: Array<File | (Blob & { name: string; lastModified?: number })>): string[] {
    const rejected: string[] = [];
    for (const f of files) {
      if (!mediaTypeOf(f.name)) {
        rejected.push(f.name);
        continue;
      }
      const key = fileKey(f);
      const existing = this.items.find((i) => i.key === key);
      if (existing && !["done", "failed", "cancelled"].includes(existing.state)) continue;
      this.files.set(key, f);
      this.items = this.items.filter((i) => i.key !== key);
      this.items.push({ key, name: f.name, size: f.size, sent: 0, state: "queued" });
    }
    this.emit();
    void this.pump();
    return rejected;
  }

  async cancel(key: string) {
    const it = this.items.find((i) => i.key === key);
    if (!it) return;
    this.aborts.get(key)?.abort();
    const saved = this.store.get(key);
    const id = it.sessionId ?? saved?.id;
    this.update(key, { state: "cancelled" });
    this.store.delete(key);
    this.hasher.drop(key);
    if (id) await this.transport.cancel(id).catch(() => {});
  }

  dismiss(key: string) {
    this.items = this.items.filter((i) => i.key !== key || !["done", "failed", "cancelled"].includes(i.state));
    this.emit();
  }

  private async pump() {
    if (this.running) return;
    this.running = true;
    try {
      for (;;) {
        const next = this.items.find((i) => i.state === "queued");
        if (!next) break;
        await this.run(next.key);
      }
    } finally {
      this.running = false;
    }
  }

  private async session(key: string, file: Blob & { name: string; type?: string }): Promise<UploadSessionInfo> {
    const saved = this.store.get(key);
    if (saved) {
      try {
        const s = await this.transport.status(saved.id);
        if (s.status === "active" || s.status === "completed") return s;
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 404)) throw e;
      }
      this.store.delete(key);
    }
    const idem = saved?.idem ?? (globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`);
    const s = await this.transport.create({ name: file.name, size: file.size, type: mediaTypeOf(file.name)! }, idem);
    this.store.set(key, { id: s.id, idem });
    return s;
  }

  private async run(key: string) {
    const file = this.files.get(key) as Blob & { name: string };
    let attempt = 0;
    for (;;) {
      const item = this.items.find((i) => i.key === key);
      if (!item || item.state === "cancelled") return;
      const ac = new AbortController();
      this.aborts.set(key, ac);
      try {
        this.update(key, { state: "uploading", error: undefined });
        const s = await this.session(key, file);
        this.update(key, { sessionId: s.id, sent: s.received_bytes });
        if (s.status !== "completed") {
          let offset = s.received_bytes;
          for (const [start, end] of planChunks(file.size, s.chunk_size, offset)) {
            const digest = await this.hasher.chunk(key, file, start, end);
            if (this.items.find((i) => i.key === key)?.state === "cancelled") return;
            try {
              offset = await this.transport.chunk(s.id, start, digest, file.slice(start, end), ac.signal);
            } catch (e) {
              if (e instanceof ApiError && e.code === "offset_mismatch") {
                // Another tab or a lost response moved the server on: resync and continue.
                throw new ResyncError();
              }
              throw e;
            }
            attempt = 0;
            this.update(key, { sent: offset });
          }
        }
        this.update(key, { state: "finalizing", sent: file.size });
        const sha = s.status === "completed" ? "" : await this.hasher.finish(key, file);
        const res = await this.transport.finalize(s.id, sha);
        this.store.delete(key);
        this.update(key, { state: "done", title: res.title ?? null });
        this.onDone?.(this.items.find((i) => i.key === key)!);
        return;
      } catch (e) {
        if (this.items.find((i) => i.key === key)?.state === "cancelled") return;
        if (e instanceof ResyncError) continue;
        if (e instanceof ApiError && e.status === 410) {
          // Expired or failed on the server: start a fresh session for the same file once.
          this.store.delete(key);
          this.hasher.drop(key);
          if (attempt++ < 1) continue;
        }
        if (isRetryable(e) && attempt < this.maxAttempts) {
          this.update(key, { state: "retrying", error: (e as Error).message });
          await this.sleep(backoff(attempt++));
          continue;
        }
        this.store.delete(key);
        this.hasher.drop(key);
        this.update(key, { state: "failed", error: (e as Error).message });
        return;
      }
    }
  }
}

class ResyncError extends Error {}

// --- browser wiring ----------------------------------------------------------------------

export function workerHasher(): Hasher {
  const worker = new Worker(new URL("./hash.worker.ts", import.meta.url), { type: "module" });
  let seq = 0;
  const waiting = new Map<number, { resolve: (v: string) => void; reject: (e: Error) => void }>();
  worker.onmessage = (e: MessageEvent<{ id: number; hex?: string; error?: string }>) => {
    const w = waiting.get(e.data.id);
    waiting.delete(e.data.id);
    if (!w) return;
    if (e.data.error) w.reject(new Error(e.data.error));
    else w.resolve(e.data.hex ?? "");
  };
  const call = (msg: Record<string, unknown>) =>
    new Promise<string>((resolve, reject) => {
      const id = ++seq;
      waiting.set(id, { resolve, reject });
      worker.postMessage({ id, ...msg });
    });
  return {
    chunk: (key, file, start, end) => call({ op: "chunk", key, file, start, end }),
    finish: (key, file) => call({ op: "finish", key, file }),
    drop: (key) => void call({ op: "drop", key }),
  };
}

export function localStore(): Store {
  const KEY = "lectern.uploads";
  const read = (): Record<string, { id: string; idem: string }> => {
    try {
      return JSON.parse(localStorage.getItem(KEY) || "{}");
    } catch {
      return {};
    }
  };
  const write = (v: Record<string, { id: string; idem: string }>) => {
    try {
      localStorage.setItem(KEY, JSON.stringify(v));
    } catch {
      /* quota */
    }
  };
  return {
    get: (k) => read()[k],
    set: (k, v) => write({ ...read(), [k]: v }),
    delete: (k) => {
      const all = read();
      delete all[k];
      write(all);
    },
  };
}

export function httpTransport(): Transport {
  return {
    create: (f, idem) =>
      post<UploadSessionInfo>("/api/uploads", { filename: f.name, size: f.size, media_type: f.type }, { "idempotency-key": idem }),
    status: (id) => get<UploadSessionInfo>(`/api/uploads/${id}`),
    chunk: (id, offset, digest, body, signal) => putChunk(id, offset, digest, body, signal),
    finalize: (id, sha256) => post(`/api/uploads/${id}/finalize`, sha256 ? { sha256 } : {}),
    cancel: async (id) => {
      await del(`/api/uploads/${id}`);
    },
  };
}
