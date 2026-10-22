/// <reference lib="webworker" />
// Lectern's service worker (docs/offline.md). Built by vite.config.ts into /app/sw.js with the
// build's version and file list compiled in, so every deploy is a new worker.
//
// - Install: precache the app shell for this version. The new worker then waits; the app
//   shows "Update ready" and sends SKIP_WAITING when the reader chooses to reload.
// - Activate: delete other versions' shell caches. Downloaded books live in their own cache and
//   survive updates.
// - Fetch: see lib/offline/routes.ts. Nothing from /api or any signed-in page is ever stored.

import { BOOKS_CACHE, parseRange, route, SHELL_PREFIX, SHELL_URL } from "../src/lib/offline/routes";

declare const self: ServiceWorkerGlobalScope;
declare const __LECTERN_VERSION__: string;
declare const __LECTERN_PRECACHE__: string[];

const VERSION = __LECTERN_VERSION__;
const SHELL = `${SHELL_PREFIX}${VERSION}`;

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL);
      // The shell route is public and identical for everyone; refuse anything else (an error
      // page or a redirect would be served offline as the app).
      const res = await fetch(SHELL_URL, { cache: "no-store", credentials: "omit" });
      if (!res.ok || res.redirected || !(res.headers.get("content-type") ?? "").includes("text/html")) throw new Error(`shell ${res.status}`);
      await cache.put(SHELL_URL, res);
      await cache.addAll(__LECTERN_PRECACHE__.map((u) => new Request(u, { credentials: "omit" })));
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      for (const name of await caches.keys()) if (name.startsWith(SHELL_PREFIX) && name !== SHELL) await caches.delete(name);
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("message", (event) => {
  const data = event.data as { type?: string } | null;
  if (data?.type === "SKIP_WAITING") self.skipWaiting();
  else if (data?.type === "VERSION") event.ports[0]?.postMessage({ version: VERSION });
});

self.addEventListener("fetch", (event) => {
  const r = route(event.request, self.location.origin);
  if (r.kind === "network") return;
  if (r.kind === "navigate") event.respondWith(navigate(event.request));
  else if (r.kind === "static") event.respondWith(staticAsset(event.request));
  else event.respondWith(book(event.request, r.key));
});

async function navigate(req: Request): Promise<Response> {
  try {
    return await fetch(req);
  } catch {
    const shell = await caches.open(SHELL).then((c) => c.match(SHELL_URL));
    return shell ?? new Response("Lectern is offline and this device has no copy of the app yet.", { status: 503, headers: { "content-type": "text/plain; charset=utf-8" } });
  }
}

async function staticAsset(req: Request): Promise<Response> {
  const cache = await caches.open(SHELL);
  const hit = await cache.match(req, { ignoreVary: true });
  if (hit) return hit;
  const res = await fetch(req);
  // Public build output only (routes.ts); store it for offline use of this version.
  if (res.ok && res.status === 200 && !res.redirected) await cache.put(req, res.clone());
  return res;
}

async function book(req: Request, key: string): Promise<Response> {
  const cache = await caches.open(BOOKS_CACHE);
  const hit = await cache.match(key);
  if (!hit) return fetch(req);
  return withRange(hit, req.headers.get("range"));
}

/** Serve a stored file, honouring a single Range (audio seeking, pdf.js) like the server does. */
async function withRange(stored: Response, header: string | null): Promise<Response> {
  const blob = await stored.blob();
  const type = stored.headers.get("content-type") || "application/octet-stream";
  const base: Record<string, string> = { "content-type": type, "accept-ranges": "bytes", "cache-control": "no-store", "x-lectern-offline": "1" };
  const range = parseRange(header, blob.size);
  if (range === "unsatisfiable") return new Response(null, { status: 416, headers: { ...base, "content-range": `bytes */${blob.size}` } });
  if (!range) return new Response(blob, { status: 200, headers: { ...base, "content-length": String(blob.size) } });
  const part = blob.slice(range.start, range.end + 1);
  return new Response(part, {
    status: 206,
    headers: { ...base, "content-length": String(part.size), "content-range": `bytes ${range.start}-${range.end}/${blob.size}` },
  });
}
