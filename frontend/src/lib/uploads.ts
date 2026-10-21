// Resumable uploads (docs/contracts.md "Resumable uploads"): create a session, send ≤ 50 MiB
// chunks at the server's offset with a per-chunk SHA-256, resume from the server's offset after
// any failure, and finalize with the whole-file SHA-256. Survives flaky home uplinks and, with
// the same file picked again, a page reload.

import { ApiError, del, get, post, putChunk } from "./api";
import type { ImportInfo, UploadSessionInfo } from "./types";

export type UploadState = "queued" | "uploading" | "paused" | "retrying" | "finalizing" | "done" | "failed" | "cancelled";
const TERMINAL: UploadState[] = ["done", "failed", "cancelled"];
export const isTerminal = (s: UploadState) => TERMINAL.includes(s);

export interface UploadItem {
  key: string;
  name: string;
  size: number;
  sent: number;
  state: UploadState;
  error?: string;
  title?: string | null;
  sessionId?: string;
  /** Files added together that make one book (one import on the server). */
  group?: string;
  /** Set when the server already has these exact bytes. */
  duplicateOf?: { work_id: number; title?: string } | null;
}

export interface FinalizeResult {
  title?: string | null;
  queued?: string | null;
  file?: { id: number; status: "ok" | "duplicate"; duplicate_work_id: number | null };
}

/** What the manager needs from the server; the real one wraps fetch, tests use a fake. */
export interface Transport {
  create(file: { name: string; size: number; type: string }, idempotencyKey: string, importId?: string): Promise<UploadSessionInfo>;
  status(id: string): Promise<UploadSessionInfo>;
  chunk(id: string, offset: number, digest: string, body: Blob, signal: AbortSignal): Promise<number>;
  finalize(id: string, sha256: string): Promise<FinalizeResult>;
  cancel(id: string): Promise<void>;
  /** Review before filing. Without these, uploads go straight to the inbox as before. */
  createImport?(label: string, idempotencyKey: string): Promise<ImportInfo>;
  completeImport?(id: string): Promise<ImportInfo>;
  discardImport?(id: string): Promise<void>;
}

interface Group {
  id: string;
  label: string;
  idem: string;
  importId?: string;
  closed?: boolean;
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
  pdf: "application/pdf",
  cbz: "application/vnd.comicbook+zip",
  mp3: "audio/mpeg",
  m4b: "audio/mp4",
  m4a: "audio/mp4",
  mp4: "audio/mp4",
  ogg: "audio/ogg",
  oga: "audio/ogg",
  opus: "audio/ogg",
  flac: "audio/flac",
};
export const ACCEPT = ".epub,.pdf,.cbz,.m4b,.m4a,.mp3,.ogg,.opus,.oga,.flac,application/epub+zip,application/pdf,application/vnd.comicbook+zip,audio/mpeg,audio/mp4,audio/ogg,audio/flac";

export function mediaTypeOf(name: string): string | null {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  return MEDIA[ext] ?? null;
}

export function fileKey(f: { name: string; size: number; lastModified?: number; relPath?: string; webkitRelativePath?: string }): string {
  // Folder uploads repeat names ("CD 1/01.mp3", "CD 2/01.mp3"): the path keeps them apart.
  const path = f.relPath || f.webkitRelativePath || "";
  return `${path && path !== f.name ? `${path}|` : ""}${f.name}:${f.size}:${f.lastModified ?? 0}`;
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

const uuid = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;

export function humanBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v >= 10 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

/** A file plus where it sat in a picked or dropped folder ("Book/CD1/01.mp3"). */
export type PathedFile = (File | (Blob & { name: string; lastModified?: number })) & { webkitRelativePath?: string; relPath?: string };

const DISC = /^(cd|disc|disk|part|teil|volume|vol)[\s._-]*\d+$/i;
const LOOSE_AUDIO = /\.(mp3|ogg|oga|opus|flac)$/i;

function dirOf(f: PathedFile): string[] {
  const p = f.relPath || f.webkitRelativePath || "";
  const parts = p.split("/").filter(Boolean);
  parts.pop();
  // "Book/CD 1/01.mp3" and "Book/CD 2/01.mp3" are one book.
  while (parts.length > 1 && DISC.test(parts[parts.length - 1])) parts.pop();
  return parts;
}

/**
 * Which files are one book. Each EPUB, PDF, comic (CBZ), and single-file audiobook (M4B) is its
 * own book.
 * Audio tracks that share a folder are one audiobook, with that folder's EPUB if it has exactly
 * one; loose MP3/OGG/FLAC tracks dropped together are one audiobook. The review step can still
 * split or combine anything this gets wrong.
 */
export function groupFiles(files: PathedFile[]): Array<{ label: string; files: PathedFile[] }> {
  const byDir = new Map<string, PathedFile[]>();
  for (const f of files) {
    const d = dirOf(f).join("/");
    byDir.set(d, [...(byDir.get(d) ?? []), f]);
  }
  const out: Array<{ label: string; files: PathedFile[] }> = [];
  const stem = (n: string) => n.replace(/\.[^.]+$/, "");
  for (const [dir, list] of byDir) {
    const epubs = list.filter((f) => /\.epub$/i.test(f.name));
    const tracks = list.filter((f) => LOOSE_AUDIO.test(f.name));
    const singles = list.filter((f) => !epubs.includes(f) && !tracks.includes(f));
    const folder = dir.split("/").pop() || "";
    if (tracks.length) {
      const withBook = dir && epubs.length === 1 ? epubs.splice(0, 1) : [];
      const sorted = [...tracks].sort((a, b) => (a.relPath || a.webkitRelativePath || a.name).localeCompare(b.relPath || b.webkitRelativePath || b.name, undefined, { numeric: true }));
      out.push({ label: folder || (tracks.length === 1 ? stem(tracks[0].name) : `${tracks.length} audio files`), files: [...withBook, ...sorted] });
    }
    for (const f of [...epubs, ...singles]) out.push({ label: stem(f.name), files: [f] });
  }
  return out;
}

/** Folder drops: walk the dropped entries (Chrome, Edge, Firefox, Safari all expose them). */
export async function filesFromDataTransfer(dt: DataTransfer): Promise<PathedFile[]> {
  type Entry = { isFile: boolean; isDirectory: boolean; name: string; fullPath: string; file?: (cb: (f: File) => void, err: (e: unknown) => void) => void; createReader?: () => { readEntries: (cb: (e: Entry[]) => void, err: (e: unknown) => void) => void } };
  const entries: Entry[] = [];
  for (const item of Array.from(dt.items ?? [])) {
    const e = (item as DataTransferItem & { webkitGetAsEntry?: () => Entry | null }).webkitGetAsEntry?.();
    if (e) entries.push(e);
  }
  if (!entries.length) return Array.from(dt.files);
  const out: PathedFile[] = [];
  const walk = async (e: Entry, depth: number): Promise<void> => {
    if (out.length > 2000 || depth > 6) return;
    if (e.isFile && e.file) {
      const f = await new Promise<File>((res, rej) => e.file!(res, rej));
      out.push(Object.assign(f, { relPath: e.fullPath.replace(/^\//, "") }));
    } else if (e.isDirectory && e.createReader) {
      const reader = e.createReader();
      for (;;) {
        const batch = await new Promise<Entry[]>((res, rej) => reader.readEntries(res, rej));
        if (!batch.length) break;
        for (const c of batch) await walk(c, depth + 1);
      }
    }
  };
  for (const e of entries) await walk(e, 0);
  return out;
}

type Listener = (items: UploadItem[]) => void;

export class UploadManager {
  items: UploadItem[] = [];
  private files = new Map<string, File | Blob>();
  private aborts = new Map<string, AbortController>();
  private listeners = new Set<Listener>();
  private groups = new Map<string, Group>();
  private running = false;
  /** Largest file the server accepts; larger ones fail at once with a plain message. */
  maxBytes = Infinity;
  onDone?: (item: UploadItem) => void;
  /** Every file of a book finished; the import is ready for review (or already filed). */
  onImportReady?: (info: ImportInfo, group: string) => void;
  onImportError?: (label: string, err: Error) => void;

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

  /**
   * Queue files. Unsupported types are reported and skipped; returns the rejected names. With
   * ``book``, the files are one book: they share one import (review before filing).
   */
  add(files: Array<PathedFile>, book?: { label: string }): string[] {
    const rejected: string[] = [];
    let group: Group | undefined;
    if (book && this.transport.createImport) {
      group = { id: uuid(), label: book.label, idem: uuid() };
      this.groups.set(group.id, group);
    }
    for (const f of files) {
      if (!mediaTypeOf(f.name)) {
        rejected.push(f.name);
        continue;
      }
      const key = fileKey(f);
      const existing = this.items.find((i) => i.key === key);
      if (existing && !isTerminal(existing.state)) continue;
      this.files.set(key, f);
      this.items = this.items.filter((i) => i.key !== key);
      const tooBig = f.size > this.maxBytes;
      this.items.push({
        key,
        name: f.name,
        size: f.size,
        sent: 0,
        state: tooBig ? "failed" : "queued",
        group: group?.id,
        error: tooBig ? `Too large: ${humanBytes(f.size)}. This server accepts files up to ${humanBytes(this.maxBytes)}.` : undefined,
      });
    }
    this.emit();
    if (group) void this.maybeClose(group.id);
    void this.pump();
    return rejected;
  }

  /** Drops a book's finished rows once it has moved on to review. */
  clearDone(groupId: string) {
    this.items = this.items.filter((i) => i.group !== groupId || i.state !== "done");
    this.emit();
  }

  groupLabel(id: string | undefined): string | undefined {
    return id ? this.groups.get(id)?.label : undefined;
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
    await this.maybeClose(it.group);
  }

  /** Stop sending; the server keeps what it has, and ``resume`` continues from there. */
  pause(key: string) {
    const it = this.items.find((i) => i.key === key);
    if (!it || isTerminal(it.state) || it.state === "finalizing") return;
    this.update(key, { state: "paused" });
    this.aborts.get(key)?.abort();
  }

  resume(key: string) {
    const it = this.items.find((i) => i.key === key);
    if (it?.state !== "paused") return;
    this.update(key, { state: "queued" });
    void this.pump();
  }

  /** Try a failed or cancelled file again. Once its book went to review, it starts a new one. */
  retry(key: string) {
    const it = this.items.find((i) => i.key === key);
    if (!it || (it.state !== "failed" && it.state !== "cancelled") || !this.files.has(key)) return;
    const f = this.files.get(key)!;
    if (f.size > this.maxBytes) return;
    let group = it.group;
    const g = group ? this.groups.get(group) : undefined;
    if (g?.closed) {
      const fresh: Group = { id: uuid(), label: g.label, idem: uuid() };
      this.groups.set(fresh.id, fresh);
      group = fresh.id;
    }
    this.update(key, { state: "queued", error: undefined, sent: 0, group, duplicateOf: null });
    void this.pump();
  }

  dismiss(key: string) {
    this.items = this.items.filter((i) => i.key !== key || !isTerminal(i.state));
    this.emit();
  }

  /** When every file of a book has finished, hand the import over for review (or discard it). */
  private async maybeClose(groupId: string | undefined) {
    const g = groupId ? this.groups.get(groupId) : undefined;
    if (!g || g.closed) return;
    const members = this.items.filter((i) => i.group === g.id);
    if (members.some((i) => !isTerminal(i.state))) return;
    g.closed = true;
    try {
      if (!members.some((i) => i.state === "done")) {
        if (g.importId) await this.transport.discardImport?.(g.importId);
        return;
      }
      const info = await this.transport.completeImport!(g.importId!);
      this.onImportReady?.(info, g.id);
    } catch (e) {
      this.onImportError?.(g.label, e as Error);
    }
  }

  private async importFor(groupId: string | undefined): Promise<string | undefined> {
    const g = groupId ? this.groups.get(groupId) : undefined;
    if (!g) return undefined;
    if (!g.importId) g.importId = (await this.transport.createImport!(g.label, g.idem)).id;
    return g.importId;
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

  private async session(key: string, file: Blob & { name: string; type?: string }, importId?: string): Promise<UploadSessionInfo> {
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
    const idem = saved?.idem ?? uuid();
    const s = await this.transport.create({ name: file.name, size: file.size, type: mediaTypeOf(file.name)! }, idem, importId);
    this.store.set(key, { id: s.id, idem });
    return s;
  }

  private async run(key: string) {
    const file = this.files.get(key) as Blob & { name: string };
    let attempt = 0;
    const stopped = () => {
      const st = this.items.find((i) => i.key === key)?.state;
      return st === "cancelled" || st === "paused";
    };
    for (;;) {
      const item = this.items.find((i) => i.key === key);
      if (!item || item.state === "cancelled" || item.state === "paused") return;
      const ac = new AbortController();
      this.aborts.set(key, ac);
      try {
        this.update(key, { state: "uploading", error: undefined });
        const importId = await this.importFor(item.group);
        const s = await this.session(key, file, importId);
        this.update(key, { sessionId: s.id, sent: s.received_bytes });
        if (s.status !== "completed") {
          let offset = s.received_bytes;
          for (const [start, end] of planChunks(file.size, s.chunk_size, offset)) {
            const digest = await this.hasher.chunk(key, file, start, end);
            if (stopped()) return;
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
        const dup = res.file?.status === "duplicate" && res.file.duplicate_work_id != null ? { work_id: res.file.duplicate_work_id } : null;
        this.update(key, { state: "done", title: res.title ?? null, duplicateOf: dup });
        this.onDone?.(this.items.find((i) => i.key === key)!);
        await this.maybeClose(item.group);
        return;
      } catch (e) {
        if (stopped()) return;
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
        this.update(key, { state: "failed", error: plainError(e) });
        await this.maybeClose(item.group);
        return;
      }
    }
  }
}

class ResyncError extends Error {}

/** What went wrong, in words that say what to do next. */
export function plainError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.code === "unsupported_media_type") return "Wrong format. Lectern takes EPUB, PDF, and CBZ comics, and M4B, MP3, OGG/Opus, or FLAC audiobooks. Convert CBR comics to CBZ first.";
    if (e.code === "not_epub") return "Wrong format: this is not a readable EPUB. It may be damaged or DRM-protected.";
    if (e.code === "not_pdf") return "Wrong format: this is not a readable PDF.";
    if (e.code === "pdf_encrypted") return "This PDF is password-protected. Remove the password and upload it again.";
    if (e.code === "pdf_too_complex") return "This PDF is too large or complex to open safely.";
    if (e.code === "not_comic") return "Wrong format: this is not a CBZ comic (a zip of page images).";
    if (e.code === "comic_encrypted") return "This comic archive is password-protected.";
    if (e.code === "archive_too_large") return "This comic archive is too large or unpacks to too much data.";
    if (e.code === "too_many_pages") return "This book has more pages than Lectern accepts.";
    if (e.code === "not_audio") return "Wrong format: Lectern could not read this as audio.";
    if (e.status === 413 || e.code === "too_large") return "Too large for this server. Ask the owner to raise the upload limit.";
  }
  if (e instanceof TypeError) return "Connection lost. Retry when you are back online.";
  return (e as Error)?.message || "Upload failed";
}

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
    create: (f, idem, importId) =>
      post<UploadSessionInfo>("/api/uploads", { filename: f.name, size: f.size, media_type: f.type, ...(importId ? { import_id: importId } : {}) }, { "idempotency-key": idem }),
    status: (id) => get<UploadSessionInfo>(`/api/uploads/${id}`),
    chunk: (id, offset, digest, body, signal) => putChunk(id, offset, digest, body, signal),
    finalize: (id, sha256) => post(`/api/uploads/${id}/finalize`, sha256 ? { sha256 } : {}),
    cancel: async (id) => {
      await del(`/api/uploads/${id}`);
    },
    createImport: (label, idem) => post<ImportInfo>("/api/imports", { label: label.slice(0, 256) }, { "idempotency-key": idem }),
    completeImport: (id) => post<ImportInfo>(`/api/imports/${id}/complete`),
    discardImport: async (id) => {
      await del(`/api/imports/${id}`);
    },
  };
}
