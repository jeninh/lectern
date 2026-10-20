// Thin JSON client. Every write carries the per-session CSRF token; the browser adds the
// Origin header itself, which the server checks for same-site.

let csrfToken = "";

export function setCsrfToken(token: string) {
  csrfToken = token;
}

export function csrf(): string {
  return csrfToken;
}

/** Friendlier text for the machine-readable error codes the API returns. */
const MESSAGES: Record<string, string> = {
  download_cap: "Download limit reached for this hour.",
  too_large: "That file is larger than this server accepts.",
  upload_quota: "You have too much uploading at once. Finish or cancel an upload first.",
  too_many_uploads: "Five uploads are already in progress. Finish or cancel one first.",
  unsupported_media_type: "Upload EPUB, M4B, M4A, MP3, OGG/Opus, or FLAC files.",
  insufficient_storage: "The server is out of disk space for uploads.",
  checksum_mismatch: "The file changed or was corrupted on the way. Try again.",
  not_epub: "That is not an EPUB file.",
  not_audio: "That is not an audio file Lectern can read.",
  variant_unavailable: "No data-saver copy yet.",
  ffmpeg_unavailable: "ffmpeg is not installed on the server.",
  no_sentences: "This book has no text to place a position in.",
  duplicate_name: "You already have a shelf with that name.",
  only_owner_can_raise_cap: "You can only lower this device's limit. Ask the owner to raise it.",
  only_owner_grants_readers: "Only the owner can share shelves with readers.",
  shelf_shared_with_readers: "Other readers can see this shelf, so only the owner can add books to it.",
};

export class ApiError extends Error {
  status: number;
  /** The API's machine-readable code (``detail``), when there is one. */
  code: string | null;
  data: Record<string, unknown> | null;
  headers: Headers | null;
  constructor(status: number, message: string, code: string | null = null, data: Record<string, unknown> | null = null, headers: Headers | null = null) {
    super(message);
    this.status = status;
    this.code = code;
    this.data = data;
    this.headers = headers;
  }
}

async function parse(res: Response) {
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    if (res.status === 401) {
      window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
    }
    const d = data as { detail?: unknown; message?: string } | null;
    const code = typeof d?.detail === "string" ? d.detail : null;
    const message = d?.message || (code && MESSAGES[code]) || code || `Request failed (${res.status})`;
    throw new ApiError(res.status, message, code, (data as Record<string, unknown>) ?? null, res.headers);
  }
  return data;
}

export async function get<T>(path: string): Promise<T> {
  const res = await fetch(path, { credentials: "same-origin", headers: { accept: "application/json" } });
  return (await parse(res)) as T;
}

async function send<T>(method: string, path: string, body?: unknown, extra?: HeadersInit, keepalive = false): Promise<T> {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    keepalive,
    headers: {
      accept: "application/json",
      "content-type": "application/json",
      "x-csrf-token": csrfToken,
      ...extra,
    },
    body: method === "DELETE" && body === undefined ? undefined : JSON.stringify(body ?? {}),
  });
  return (await parse(res)) as T;
}

export const post = <T>(path: string, body?: unknown, headers?: HeadersInit) => send<T>("POST", path, body, headers);
export const put = <T>(path: string, body?: unknown, keepalive = false) => send<T>("PUT", path, body, undefined, keepalive);
export const patch = <T>(path: string, body?: unknown) => send<T>("PATCH", path, body);
export const del = <T>(path: string) => send<T>("DELETE", path);

/** Raw chunk PUT for resumable uploads; returns the server's new offset. */
export async function putChunk(id: string, offset: number, digest: string, body: Blob, signal?: AbortSignal): Promise<number> {
  const res = await fetch(`/api/uploads/${id}/chunk`, {
    method: "PUT",
    credentials: "same-origin",
    signal,
    headers: {
      "content-type": "application/octet-stream",
      "x-csrf-token": csrfToken,
      "upload-offset": String(offset),
      "x-chunk-sha256": digest,
    },
    body,
  });
  if (res.status === 204) return Number(res.headers.get("upload-offset"));
  await parse(res);
  throw new ApiError(res.status, `Unexpected ${res.status}`);
}

export function logout() {
  const form = document.createElement("form");
  form.method = "post";
  form.action = "/logout";
  const input = document.createElement("input");
  input.type = "hidden";
  input.name = "csrf_token";
  input.value = csrfToken;
  form.appendChild(input);
  document.body.appendChild(form);
  form.submit();
}
