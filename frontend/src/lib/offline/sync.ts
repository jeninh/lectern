// Saving positions with the offline queue behind it, and the service worker's registration and
// "Update ready" state. Wired into the app by lib/store.tsx.

import { ApiError, csrf, OfflineError, put } from "../api";
import { idb } from "./db";
import { isOnline } from "./net";
import { keyOf, serverTime, type PositionKind, PositionQueue, type QueuedPosition, type ServerPosition, type Transport } from "./queue";

// --- positions -------------------------------------------------------------------------------

const URLS: Record<PositionKind, (id: number) => string> = {
  work: (id) => `/api/works/${id}/progress`,
  page: (id) => `/api/files/${id}/progress`,
};

/** The localStorage mark each viewer keeps per book (Reader, player, PageReader). */
export function markKey(kind: PositionKind, id: number, body: Record<string, unknown>): string {
  if (kind === "page") return `lectern.page.${id}`;
  return body.source === "audio" ? `lectern.listen.${id}` : `lectern.read.${id}`;
}

function readSeenAt(key: string): string | null {
  try {
    return (JSON.parse(localStorage.getItem(key) || "null") as { seenAt?: string | null } | null)?.seenAt ?? null;
  } catch {
    return null;
  }
}

function patchMark(key: string, patch: Record<string, unknown>) {
  try {
    const prev = JSON.parse(localStorage.getItem(key) || "null") ?? {};
    localStorage.setItem(key, JSON.stringify({ ...prev, ...patch }));
  } catch {
    /* quota */
  }
}

const send: Transport = async (e, { sentAt, keep, expect }) => {
  try {
    const offline = { recorded_at: e.recordedAt, sent_at: sentAt, base: e.base, ...(keep ? { keep, expect: expect ?? null } : {}) };
    const body = { ...e.body, offline };
    const r = await put<ServerPosition>(URLS[e.kind](e.id), body);
    return { ok: true, seenAt: r.received_at ?? r.updated_at ?? null, server: r };
  } catch (err) {
    if (err instanceof ApiError && err.code === "progress_conflict") {
      const d = err.data as { server?: ServerPosition | null; recorded_at?: string | null } | null;
      return { ok: false, conflict: true, server: d?.server ?? null, recordedAt: d?.recorded_at ?? null };
    }
    // 401 signs out (api.ts); 404/409/422 will never be accepted; anything else: try later.
    const retry = !(err instanceof ApiError) || err.status >= 500 || err.status === 429 || err.status === 403;
    return { ok: false, conflict: false, retry };
  }
};

export const queue = new PositionQueue(
  {
    all: () => idb.all<QueuedPosition>("queue").catch(() => []),
    put: (e) => idb.put("queue", e.key, e),
    del: (k) => idb.del("queue", k),
  },
  send,
  (e, seenAt) => {
    if (seenAt) patchMark(markKey(e.kind, e.id, e.body), { seenAt });
    emit();
  },
);

export interface SaveTarget {
  kind: PositionKind;
  id: number;
  workId: number;
  title: string;
}

/**
 * Save a position: straight to the server when it can be reached, otherwise into the queue.
 * Resolves to the server's answer, or null when it was queued. A save that reaches the server
 * replaces anything queued for the same book (it is newer).
 */
export async function savePosition<T>(t: SaveTarget, body: Record<string, unknown>, keepalive = false): Promise<T | null> {
  const enqueue = async () => {
    await queue.add({ ...t, body, recordedAt: new Date().toISOString(), base: readSeenAt(markKey(t.kind, t.id, body)) });
    emit();
    return null;
  };
  if (!isOnline() || !csrf()) return enqueue();
  const savedAt = new Date().toISOString();
  try {
    const r = await put<T>(URLS[t.kind](t.id), body, keepalive);
    // The server took a newer position: anything queued before it is moot (compare-and-set, so
    // a flush answering meanwhile cannot bring it back; lib/offline/queue.ts).
    if ((await queue.list()).some((e) => e.key === keyOf(t.kind, t.id))) {
      await queue.drop(t.kind, t.id, savedAt);
      emit();
    }
    return r;
  } catch (e) {
    if (e instanceof OfflineError) return enqueue();
    throw e;
  }
}

/** A conflict answered with "use the other device's": move this device's mark there too, and
 * tell an open reader or player (event `lectern:position`). */
export async function resolveConflict(e: QueuedPosition, choice: "mine" | "theirs"): Promise<"done" | "again"> {
  const server = await queue.resolve(e.key, choice);
  // "Keep mine" can meet a position saved after the prompt opened: the prompt asks again.
  const again = (await queue.list()).some((x) => x.key === e.key && x.conflict && serverTime(x.conflict.server) !== serverTime(e.conflict?.server));
  if (choice === "theirs" && server) {
    const key = markKey(e.kind, e.id, e.body);
    if (e.kind === "page") patchMark(key, { page: server.page ?? 1 });
    else if (e.body.source === "audio") patchMark(key, { t: server.audio_s ?? 0, ...(server.speed != null ? { rate: server.speed } : {}) });
    else patchMark(key, { cfi: server.source === "web" ? server.raw ?? "" : "" });
    window.dispatchEvent(new CustomEvent("lectern:position", { detail: { kind: e.kind, id: e.id, server } }));
  }
  await emit();
  return again ? "again" : "done";
}

// --- state for the UI ------------------------------------------------------------------------

export interface SyncState {
  pending: number;
  conflicts: QueuedPosition[];
  syncing: boolean;
}

let state: SyncState = { pending: 0, conflicts: [], syncing: false };
const listeners = new Set<(s: SyncState) => void>();

export function syncState(): SyncState {
  return state;
}
export function onSync(fn: (s: SyncState) => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

async function emit() {
  const all = await queue.list();
  state = { ...state, pending: all.filter((e) => !e.conflict).length, conflicts: all.filter((e) => e.conflict) };
  for (const l of listeners) l(state);
}

let flushing: Promise<void> | null = null;
/** Send queued positions (on reconnect, focus, and start). Needs a session (CSRF token). */
export function flush(): Promise<void> {
  if (flushing) return flushing;
  flushing = (async () => {
    try {
      if (!csrf() || !isOnline()) return;
      if (!(await queue.list()).some((e) => !e.conflict)) return;
      state = { ...state, syncing: true };
      await emit();
      await queue.flush();
    } finally {
      state = { ...state, syncing: false };
      await emit();
      flushing = null;
    }
  })();
  return flushing;
}

export const refreshSyncState = emit;

// --- service worker ----------------------------------------------------------------------------

type UpdateListener = (ready: boolean) => void;
let waiting: ServiceWorker | null = null;
const updateListeners = new Set<UpdateListener>();

export function onUpdateReady(fn: UpdateListener): () => void {
  updateListeners.add(fn);
  fn(!!waiting);
  return () => updateListeners.delete(fn);
}

function setWaiting(w: ServiceWorker | null) {
  waiting = w;
  for (const l of updateListeners) l(!!w);
}

/** Register /app/sw.js. A new version installs in the background and waits; `applyUpdate`
 * activates it and reloads once it controls the page. */
export async function registerWorker(): Promise<ServiceWorkerRegistration | null> {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return null;
  try {
    const reg = await navigator.serviceWorker.register("/app/sw.js", { scope: "/app", updateViaCache: "none" });
    const track = (w: ServiceWorker | null) => {
      if (!w) return;
      const check = () => w.state === "installed" && navigator.serviceWorker.controller && setWaiting(w);
      check();
      w.addEventListener("statechange", check);
    };
    track(reg.waiting);
    track(reg.installing);
    reg.addEventListener("updatefound", () => track(reg.installing));
    // Check for a new version when the app comes back to the foreground (phones rarely reload).
    document.addEventListener("visibilitychange", () => !document.hidden && isOnline() && reg.update().catch(() => {}));
    return reg;
  } catch {
    return null;
  }
}

export function applyUpdate() {
  if (!waiting) return;
  let reloaded = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (reloaded) return;
    reloaded = true;
    window.location.reload();
  });
  waiting.postMessage({ type: "SKIP_WAITING" });
}
