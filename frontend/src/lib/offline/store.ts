// Books kept on this device for offline use (docs/offline.md).
//
// A download is the work's details, its EPUB and/or PDF, its audiobook (data saver or full
// quality), the read-along data (sentence list with audio times, and the chapter text), and the
// cover. Files go to Cache Storage under the URLs the reader and player already use, so the
// service worker answers them; the rest goes to IndexedDB. Every byte is fetched through the
// normal routes, which check that the book is visible to you and count it against the hourly
// download limits; a refused file stops the download and nothing is kept.

import { ApiError, call, csrf, get, OfflineError, post } from "../api";
import type { Me, PagesInfo, SentenceInfo, SentencesResponse, SpineTextResponse, TracksResponse, Work, WorkProgressResponse } from "../types";
import { dropDatabase, idb } from "./db";
import { BOOKS_CACHE, bookKey, DOWNLOAD_HEADER, SHELL_PREFIX } from "./routes";

export type Quality = "low" | "full";

export interface ManifestFile {
  key: string;
  kind: "epub" | "pdf";
  file_id: number;
  media_type: string;
  size: number;
  sha256: string;
  url: string;
}
export interface ManifestTrack {
  id: number;
  position: number;
  full: { url: string; size: number; media_type: string };
  low: { url: string; size: number; media_type: string } | null;
}
export interface OfflineManifest {
  work_id: number;
  revision: string;
  files: ManifestFile[];
  audio: { tracks: ManifestTrack[]; full_bytes: number; low_bytes: number | null } | null;
  readalong: boolean;
}

export interface Choice {
  epub: boolean;
  pdf: boolean;
  audio: Quality | null;
  readalong: boolean;
}

export interface OfflineBook {
  id: number;
  work: Work;
  revision: string;
  choice: Choice;
  /** What was stored: format → bytes. */
  parts: { epub?: number; pdf?: number; audio?: number; readalong?: boolean };
  /** Cache Storage keys holding this book's files. */
  keys: string[];
  bytes: number;
  downloadedAt: string;
  /** "changed": the server has a newer version (Downloads offers to update it). */
  status: "ok" | "changed";
  tracks?: TracksResponse;
  progress?: WorkProgressResponse;
  pdf?: PagesInfo;
}

export interface OfflineReadAlong {
  sentences: SentenceInfo[];
  texts: SpineTextResponse[];
}

export interface Owner {
  id: number;
  me: Omit<Me, "csrf_token"> & { csrf_token: "" };
  /** Device clock (ms): past this, without reaching the server, everything here is deleted. */
  until: number | null;
  /** The session's CSRF token, kept only so a sign-out started offline can reach /logout
   * (lib/offline/signout.ts). Never handed to the app as `me.csrf_token`. */
  csrf: string;
}

export class DownloadError extends Error {}

export function plannedBytes(m: OfflineManifest, c: Choice): number {
  let n = 0;
  for (const f of m.files) if ((f.kind === "epub" && c.epub) || (f.kind === "pdf" && c.pdf)) n += f.size;
  if (m.audio && c.audio) n += c.audio === "low" && m.audio.low_bytes != null ? m.audio.low_bytes : m.audio.full_bytes;
  return n;
}

/** Whether the data-saver copy is worth having: it exists and is smaller than the original. */
export function lowWorthIt(m: OfflineManifest): boolean {
  return !!m.audio && m.audio.low_bytes != null && m.audio.low_bytes < m.audio.full_bytes;
}

/** The default choice: the EPUB (else the PDF), the audiobook as data saver unless full quality
 * is preferred or the data-saver copy is missing or no smaller, and read-along when there is one. */
export function defaultChoice(m: OfflineManifest, fullQuality: boolean): Choice {
  return {
    epub: m.files.some((f) => f.kind === "epub"),
    pdf: m.files.some((f) => f.kind === "pdf") && !m.files.some((f) => f.kind === "epub"),
    audio: m.audio ? (fullQuality || !lowWorthIt(m) ? "full" : "low") : null,
    readalong: m.readalong,
  };
}

// --- who owns the downloads -----------------------------------------------------------------

/** Called after every successful /api/me. A different person signing in on this device never
 * sees the previous person's downloads: they are deleted first. Downloads with no recorded owner
 * (a wipe cut short by leaving the page) are deleted too. */
export async function bindOwner(me: Me): Promise<boolean> {
  try {
    const prev = await idb.get<Owner>("meta", "owner");
    let wiped = false;
    if (!prev || prev.id !== me.user.id) {
      wiped = !!prev || (typeof caches !== "undefined" && (await caches.has(BOOKS_CACHE)));
      await clearDevice({ keepWorker: true });
    }
    // The server's deadline, moved onto this device's clock.
    const until = me.offline ? Date.now() + (Date.parse(me.offline.until) - Date.parse(me.offline.now)) : null;
    await idb.put("meta", "owner", { id: me.user.id, me: { ...me, csrf_token: "" }, until, csrf: me.csrf_token } satisfies Owner);
    return wiped;
  } catch {
    return false; // private browsing without IndexedDB: offline features are simply off
  }
}

/**
 * Whether what is stored here may still be used. Downloads and the signed-in identity last
 * offline only until the deadline the server gave on the last successful /api/me (the session's
 * expiry, or `offline_max_age_seconds` if sooner); a server-side revoke cannot reach a phone
 * that stays offline, so this bounds it. Past the deadline, or with downloads nobody owns,
 * everything is deleted and this returns false.
 */
export async function checkFresh(): Promise<boolean> {
  let ok: boolean;
  try {
    const o = await idb.get<Owner>("meta", "owner");
    ok = o ? o.until != null && Date.now() <= o.until : !(typeof caches !== "undefined" && (await caches.has(BOOKS_CACHE)));
  } catch {
    return true; // no IndexedDB: nothing was stored
  }
  if (!ok) await clearDevice({ keepWorker: true });
  return ok;
}

export async function ownerSnapshot(): Promise<Owner | null> {
  if (!(await checkFresh())) return null;
  try {
    return (await idb.get<Owner>("meta", "owner")) ?? null;
  } catch {
    return null;
  }
}

/** Delete everything this app keeps on the device: downloads, read-along data, queued
 * positions, positions remembered per book, and (unless `keepWorker`) the service worker and
 * its shell caches. Used on sign-out, when the session ends, and when someone else signs in. */
export async function clearDevice({ keepWorker = false } = {}): Promise<void> {
  // The shell caches hold only the public app; they go with the worker on sign-out.
  if (typeof caches !== "undefined") {
    for (const name of await caches.keys()) if (name === BOOKS_CACHE || (!keepWorker && name.startsWith(SHELL_PREFIX))) await caches.delete(name);
  }
  await dropDatabase();
  try {
    for (const k of Object.keys(localStorage)) if (/^lectern\.(read|listen|page)\./.test(k)) localStorage.removeItem(k);
  } catch {
    /* no storage */
  }
  if (!keepWorker && "serviceWorker" in navigator) {
    for (const r of await navigator.serviceWorker.getRegistrations()) await r.unregister();
  }
  // No change notification: a listener re-reading the list would create the database again.
}

// --- reading what is stored ------------------------------------------------------------------

export async function listBooks(): Promise<OfflineBook[]> {
  if (!(await checkFresh())) return [];
  try {
    return (await idb.all<OfflineBook>("books")).sort((a, b) => a.work.sort_title.localeCompare(b.work.sort_title));
  } catch {
    return [];
  }
}

export async function getBook(id: number): Promise<OfflineBook | null> {
  if (!(await checkFresh())) return null;
  try {
    return (await idb.get<OfflineBook>("books", id)) ?? null;
  } catch {
    return null;
  }
}

export async function getReadAlong(id: number): Promise<OfflineReadAlong | null> {
  if (!(await checkFresh())) return null;
  try {
    return (await idb.get<OfflineReadAlong>("readalong", id)) ?? null;
  } catch {
    return null;
  }
}

/** The downloaded PDF's page info for the viewer, by file id. */
export async function getPdfInfo(fileId: number): Promise<PagesInfo | null> {
  return (await listBooks()).find((b) => b.pdf?.file_id === fileId)?.pdf ?? null;
}

// Components re-read the list when it changes (download, removal, check).
const listeners = new Set<() => void>();
export function onChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
function notify() {
  for (const l of listeners) l();
}

// --- storage ---------------------------------------------------------------------------------

export interface StorageInfo {
  usage: number | null;
  quota: number | null;
  persisted: boolean | null;
}

export async function storageInfo(): Promise<StorageInfo> {
  const s = navigator.storage;
  const est = s?.estimate ? await s.estimate().catch(() => null) : null;
  const persisted = s?.persisted ? await s.persisted().catch(() => null) : null;
  return { usage: est?.usage ?? null, quota: est?.quota ?? null, persisted };
}

/** Ask the browser not to evict downloads under storage pressure. Granted silently or not at
 * all (Chrome decides by engagement, an installed app usually qualifies; Safari grants it to
 * home-screen apps). */
export async function requestPersistence(): Promise<boolean> {
  try {
    return (await navigator.storage?.persist?.()) ?? false;
  } catch {
    return false;
  }
}

// --- downloading -----------------------------------------------------------------------------

export function manifestFor(workId: number): Promise<OfflineManifest> {
  return get<OfflineManifest>(`/api/works/${workId}/offline`);
}

async function fetchFile(url: string, signal: AbortSignal, onBytes: (n: number) => void): Promise<Response> {
  const res = await call(url, { credentials: "same-origin", cache: "no-store", signal, headers: { [DOWNLOAD_HEADER]: "1" } });
  if (!res.ok) {
    let data: { detail?: string; message?: string } = {};
    try {
      data = await res.json();
    } catch {
      /* not json */
    }
    if (res.status === 429) throw new DownloadError(data.message || "Download limit reached for this hour. Try again later.");
    if (res.status === 404) throw new DownloadError("This book is no longer available to you.");
    throw new ApiError(res.status, data.message || data.detail || `Download failed (${res.status})`, data.detail ?? null);
  }
  const type = res.headers.get("content-type") || "application/octet-stream";
  const reader = res.body!.getReader();
  // Streamed into Cache Storage (an audiobook can be hundreds of MB), counting as it goes.
  const counted = new ReadableStream<Uint8Array>({
    async pull(ctl) {
      const { done, value } = await reader.read();
      if (done) return ctl.close();
      onBytes(value.byteLength);
      ctl.enqueue(value);
    },
    cancel(reason) {
      return reader.cancel(reason);
    },
  });
  const headers: Record<string, string> = { "content-type": type };
  const length = res.headers.get("content-length");
  if (length) headers["content-length"] = length;
  return new Response(counted, { headers });
}

async function readAlongData(workId: number): Promise<OfflineReadAlong> {
  const base = `/api/works/${workId}`;
  const sentences: SentenceInfo[] = [];
  for (;;) {
    const page = await get<SentencesResponse>(`${base}/sentences?from=${sentences.length}&to=${sentences.length + 2000}`);
    sentences.push(...page.sentences);
    if (!page.sentences.length || sentences.length >= page.count) break;
  }
  const texts: SpineTextResponse[] = [];
  for (const i of [...new Set(sentences.map((s) => s.spine_index))]) texts.push(await get<SpineTextResponse>(`${base}/text/${i}`));
  return { sentences, texts };
}

export interface Progress {
  done: number;
  total: number;
}

const active = new Map<number, AbortController>();
export function downloading(id: number): boolean {
  return active.has(id);
}
export function cancel(id: number) {
  active.get(id)?.abort();
}

/** Download (or update) a book. Resolves to the stored record; throws DownloadError with a
 * sentence for the person when the server refuses or the device is full. */
export async function download(workId: number, choice: Choice, onProgress: (p: Progress) => void = () => {}): Promise<OfflineBook> {
  if (active.has(workId)) throw new DownloadError("Already downloading.");
  const ctl = new AbortController();
  active.set(workId, ctl);
  const previous = await getBook(workId);
  const written: string[] = [];
  const cache = await caches.open(BOOKS_CACHE);
  try {
    const parts = [choice.epub && "epub", choice.pdf && "pdf", choice.audio && `audio-${choice.audio}`, choice.readalong && "readalong"].filter(Boolean) as string[];
    const m = await post<OfflineManifest>(`/api/works/${workId}/offline`, { parts });
    const total = plannedBytes(m, choice);
    const info = await storageInfo();
    if (info.quota != null && info.usage != null && info.quota - info.usage < total * 1.05 + 5_000_000) {
      throw new DownloadError(`Not enough space on this device: this needs ${mb(total)}, about ${mb(Math.max(0, info.quota - info.usage))} is free for Lectern.`);
    }
    if (info.persisted === false) await requestPersistence();
    let done = 0;
    const tick = (n: number) => {
      done += n;
      onProgress({ done, total });
    };
    onProgress({ done, total });
    const store = async (url: string, key: string) => {
      const res = await fetchFile(url, ctl.signal, tick);
      try {
        await cache.put(key, res);
      } catch (e) {
        if ((e as Error).name === "QuotaExceededError") throw new DownloadError("This device ran out of space for downloads. Remove some and try again.");
        throw e;
      }
      written.push(key);
    };
    const record: OfflineBook = {
      id: workId,
      work: await get<Work>(`/api/works/${workId}`),
      revision: m.revision,
      choice,
      parts: {},
      keys: [],
      bytes: 0,
      downloadedAt: new Date().toISOString(),
      status: "ok",
    };
    for (const f of m.files) {
      if (!((f.kind === "epub" && choice.epub) || (f.kind === "pdf" && choice.pdf))) continue;
      const key = bookKey(new URL(f.url, location.origin));
      await store(f.url, key);
      record.parts[f.kind] = f.size;
      if (f.kind === "pdf") record.pdf = await get<PagesInfo>(`/api/files/${f.file_id}/pages`);
    }
    if (m.audio && choice.audio) {
      const tracks = await get<TracksResponse>(`/api/works/${workId}/tracks`);
      let bytes = 0;
      const urls = new Map<number, string>();
      for (const t of m.audio.tracks) {
        const v = choice.audio === "low" && t.low ? t.low : t.full;
        await store(v.url, bookKey(new URL(v.url, location.origin)));
        urls.set(t.id, v.url);
        bytes += v.size;
      }
      // The player and read-along play whatever stream_url says: point it at the stored copy.
      record.tracks = { ...tracks, tracks: tracks.tracks.map((t) => ({ ...t, stream_url: urls.get(t.id) ?? t.stream_url })) };
      record.parts.audio = bytes;
    }
    if (choice.readalong && m.readalong) {
      await idb.put("readalong", workId, await readAlongData(workId));
      record.parts.readalong = true;
    }
    if (record.work.cover_url) {
      try {
        await store(record.work.cover_url, bookKey(new URL(record.work.cover_url, location.origin)));
      } catch {
        /* a cover is nice to have */
      }
    }
    record.progress = await get<WorkProgressResponse>(`/api/works/${workId}/progress`).catch(() => undefined);
    record.keys = written;
    record.bytes = done;
    // An update may drop files the old copy had (audio switched quality, PDF unticked).
    for (const k of previous?.keys ?? []) if (!written.includes(k)) await cache.delete(k);
    if (!record.parts.readalong) await idb.del("readalong", workId);
    await idb.put("books", workId, record);
    notify();
    return record;
  } catch (e) {
    // Nothing half-downloaded is kept. An update that fails keeps the old copy's record; its
    // files may have been replaced by newer ones already, which still play.
    if (!previous) {
      for (const k of written) await cache.delete(k);
      await idb.del("readalong", workId).catch(() => {});
    }
    if ((e as Error).name === "AbortError") throw new DownloadError("Download cancelled.");
    throw e;
  } finally {
    active.delete(workId);
    notify();
  }
}

export async function remove(id: number): Promise<void> {
  const book = await getBook(id);
  if (typeof caches !== "undefined" && book) {
    const cache = await caches.open(BOOKS_CACHE);
    for (const k of book.keys) await cache.delete(k);
  }
  await idb.del("books", id);
  await idb.del("readalong", id);
  notify();
}

export async function removeAll(): Promise<void> {
  if (typeof caches !== "undefined") await caches.delete(BOOKS_CACHE);
  await idb.clear("books");
  await idb.clear("readalong");
  notify();
}

/** Ask the server whether each downloaded book is still current and still yours. Books that
 * were deleted, trashed, or unshared are removed from the device. Returns their titles. */
export async function revalidate(): Promise<{ removed: string[]; changed: number }> {
  const books = await listBooks();
  if (!books.length || !csrf()) return { removed: [], changed: 0 };
  const r = await post<{ works: { id: number; status: "ok" | "changed" | "gone"; revision?: string }[] }>("/api/offline/check", {
    works: books.map((b) => ({ id: b.id, revision: b.revision })),
  });
  const removed: string[] = [];
  let changed = 0;
  for (const res of r.works) {
    const b = books.find((x) => x.id === res.id);
    if (!b) continue;
    if (res.status === "gone") {
      await remove(b.id);
      removed.push(b.work.title);
    } else {
      const status = res.status === "changed" ? "changed" : "ok";
      if (status === "changed") changed++;
      if (status !== b.status) await idb.put("books", b.id, { ...b, status });
    }
  }
  notify();
  return { removed, changed };
}

export function mb(n: number): string {
  if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 / 1024).toFixed(n < 10 * 1024 * 1024 ? 1 : 0)} MB`;
  return `${(n / 1024 ** 3).toFixed(1)} GB`;
}

/** The server's answer, or (offline) the downloaded copy's; rethrows when there is neither. */
export async function orOffline<T>(online: () => Promise<T>, local: () => Promise<T | null | undefined>): Promise<T> {
  try {
    return await online();
  } catch (e) {
    if (!(e instanceof OfflineError)) throw e;
    const found = await local();
    if (found == null) throw new OfflineError();
    return found;
  }
}
