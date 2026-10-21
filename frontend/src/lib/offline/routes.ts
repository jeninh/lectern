// What the service worker does with each request (src/sw.ts). Pure, so it is unit-tested.
//
// The rule that keeps signed-in data private: the worker never stores a response it was not
// told to store. It precaches the app shell (public, the same for every user) and serves the
// book files the app downloaded on purpose (lib/offline/downloads.ts writes them). API
// responses, sign-in pages, OPDS, and anything else pass straight to the network and are never
// written to Cache Storage.

export const SHELL_PREFIX = "lectern-shell-";
export const BOOKS_CACHE = "lectern-books";
/** The app's HTML without the session check, for precaching (routes/spa.py). */
export const SHELL_URL = "/app/shell.html";
/** Sent by the downloader so the worker fetches fresh bytes instead of answering from a copy. */
export const DOWNLOAD_HEADER = "x-lectern-download";

export type Route =
  /** Not ours to answer: the browser's normal network behaviour. */
  | { kind: "network" }
  /** App navigation: network first (so sign-in redirects still work), the cached shell offline. */
  | { kind: "navigate" }
  /** Public, versioned build output: cache first, filled on first use. */
  | { kind: "static" }
  /** A book file, cover, or audio track: the downloaded copy if there is one, else network. */
  | { kind: "book"; key: string };

const BOOK_PATHS = [
  /^\/library\/works\/\d+\/book\.epub$/,
  /^\/library\/works\/\d+\/cover\.jpg$/,
  /^\/library\/files\/\d+\/view$/,
  /^\/library\/tracks\/\d+\/stream$/,
];

/** The Cache Storage key of a downloaded book file: path plus the query that selects content. */
export function bookKey(url: URL): string {
  const keep = new URLSearchParams();
  for (const k of ["variant", "v"]) {
    const v = url.searchParams.get(k);
    if (v != null) keep.set(k, v);
  }
  const q = keep.toString();
  return `${url.pathname}${q ? `?${q}` : ""}`;
}

export function route(req: { url: string; method: string; mode?: string; headers?: { get(name: string): string | null } }, origin: string): Route {
  const url = new URL(req.url);
  if (url.origin !== origin || req.method !== "GET") return { kind: "network" };
  if (req.headers?.get(DOWNLOAD_HEADER)) return { kind: "network" };
  const p = url.pathname;
  if (req.mode === "navigate") {
    return p === "/app" || (p.startsWith("/app/") && !p.startsWith("/app/assets/") && !p.startsWith("/app/pwa/") && !p.startsWith("/app/pdfjs/") && p !== "/app/sw.js" && p !== "/app/manifest.webmanifest")
      ? { kind: "navigate" }
      : { kind: "network" };
  }
  if (p.startsWith("/app/assets/") || p.startsWith("/app/pwa/") || p.startsWith("/app/pdfjs/") || p === "/app/manifest.webmanifest") return { kind: "static" };
  if (url.searchParams.has("offline")) return { kind: "network" };
  if (BOOK_PATHS.some((re) => re.test(p))) return { kind: "book", key: bookKey(url) };
  return { kind: "network" };
}

/** A single `bytes=` range against a body of `size` bytes, or null when the header is absent or
 * unusable (then the whole body is sent). Out-of-range starts are `"unsatisfiable"`. */
export function parseRange(header: string | null, size: number): { start: number; end: number } | "unsatisfiable" | null {
  if (!header) return null;
  const m = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
  if (!m || (m[1] === "" && m[2] === "")) return null;
  if (m[1] === "") {
    const n = Number(m[2]);
    if (n === 0) return "unsatisfiable";
    return { start: Math.max(0, size - n), end: size - 1 };
  }
  const start = Number(m[1]);
  const end = m[2] === "" ? size - 1 : Math.min(Number(m[2]), size - 1);
  if (start >= size || end < start) return "unsatisfiable";
  return { start, end };
}
