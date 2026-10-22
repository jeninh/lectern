// Thin JSON client. Every write carries the per-session CSRF token; the browser adds the
// Origin header itself, which the server checks for same-site.

import { isNetworkError, reportReachable, reportUnreachable } from "./offline/net";
import { finishSignOut, markSignOut, signOutToken } from "./offline/signout";

let csrfToken = "";
let onUnauthorized: () => Promise<void> = async () => {};

/** Run before the 401 redirect to sign-in (the installed app deletes its downloads then). */
export function setUnauthorizedHandler(fn: () => Promise<void>) {
  onUnauthorized = fn;
}

/** The server could not be reached (offline, or Tailscale off). */
export class OfflineError extends Error {
  constructor() {
    super("You are offline. This will work again once Lectern can be reached.");
    this.name = "OfflineError";
  }
}

/** fetch that keeps the online indicator current and turns network failures into OfflineError. */
export async function call(input: string, init?: RequestInit): Promise<Response> {
  try {
    const res = await fetch(input, init);
    reportReachable();
    return res;
  } catch (e) {
    if ((e as Error)?.name === "AbortError") throw e;
    if (isNetworkError(e)) {
      reportUnreachable();
      throw new OfflineError();
    }
    throw e;
  }
}

export function setCsrfToken(token: string) {
  csrfToken = token;
}

export function csrf(): string {
  return csrfToken;
}

/** Friendlier text for the machine-readable error codes the API returns. */
const MESSAGES: Record<string, string> = {
  download_cap: "Download limit reached for this hour.",
  restic_unavailable: "restic is not installed on the server. It ships in the Docker image; rebuild it.",
  backup_running: "A backup is already running.",
  too_large: "That file is larger than this server accepts.",
  upload_quota: "You have too much uploading at once. Finish or cancel an upload first.",
  too_many_uploads: "Five uploads are already in progress. Finish or cancel one first.",
  unsupported_media_type: "Upload EPUB, PDF, CBZ, DRM-free MOBI or AZW3, M4B, M4A, MP3, OGG/Opus, or FLAC files. Convert CBR comics to CBZ first.",
  drm_protected: "This book is DRM-protected; Lectern can only add DRM-free books.",
  not_mobi: "That is not a readable MOBI or AZW3 book.",
  mobi_too_complex: "That MOBI or AZW3 is too large or complex to convert safely.",
  convert_busy: "The server is busy converting other books. Try again in a moment.",
  insufficient_storage: "The server is out of disk space for uploads.",
  checksum_mismatch: "The file changed or was corrupted on the way. Try again.",
  not_epub: "That is not an EPUB file.",
  not_pdf: "That is not a readable PDF.",
  pdf_encrypted: "That PDF is password-protected. Remove the password first.",
  pdf_too_complex: "That PDF is too large or complex to open safely.",
  pdf_busy: "The server is busy reading other PDFs. Try again in a moment.",
  not_comic: "That is not a CBZ comic (a zip of page images).",
  comic_encrypted: "That comic archive is password-protected.",
  archive_too_large: "That comic archive is too large or unpacks to too much data.",
  too_many_pages: "That book has more pages than Lectern accepts.",
  needs_epub: "Needs an EPUB or a text PDF: comics and scanned PDFs have no text to read along with.",
  export_needs_epub: "The narrated EPUB is made from an EPUB edition; this book reads along on its PDF pages.",
  no_map: "This PDF's text is not ready yet.",
  not_audio: "That is not an audio file Lectern can read.",
  variant_unavailable: "No data-saver copy yet.",
  no_transcoder: "Nothing can make a data-saver copy: ffmpeg is not installed on the server and no worker that can transcode is connected.",
  data_saver_off: "Data-saver copies are turned off for this library (Admin).",
  data_saver_off_for_work: "Data-saver copies are turned off for this book (Edit → Playback).",
  no_sentences: "This book has no text to place a position in.",
  duplicate_name: "There’s already a shelf with that name there.",
  only_owner_can_raise_cap: "You can only lower this device's limit. Ask the owner to raise it.",
  only_owner_grants_readers: "Only the owner can share shelves with readers.",
  shelf_shared_with_readers: "Other readers can see this shelf, so only the owner can add books to it.",
  version_conflict: "Someone else saved this book while you were editing.",
  undo_conflict: "This book changed after that save, so it can no longer be undone.",
  undo_unavailable: "That change can no longer be undone.",
  authors_required: "Enter at least one author.",
  tags_required: "Enter at least one tag.",
  shelves_required: "Choose at least one shelf.",
  cover_not_found: "That cover upload expired. Choose the image again.",
  primary_not_an_epub_of_this_book: "Choose one of this book’s EPUBs. PDFs and comics cannot be the primary edition.",
  track_not_found: "One of those tracks no longer exists. Reload and try again.",
  file_not_found: "That file no longer exists.",
  idempotency_key_reused: "That request was already sent with different content. Try again.",
  too_many_imports: "You have many books waiting for review. File or discard some first.",
  too_many_files: "That is too many files for one book.",
  uploads_in_progress: "Some files of this book are still uploading.",
  import_closed: "This upload was already filed or discarded.",
  not_ready: "This upload is not ready to file yet.",
  target_gone: "The book you chose to join is no longer in the library.",
  target_has_audio: "That book already has an audiobook. Choose “Add as more tracks” to append, or file it as a new book.",
  nothing_to_file: "Nothing left to file: every file was removed or is a duplicate.",
  cannot_combine: "These uploads cannot be combined.",
  cover_too_large: "That image is too large for a cover (10 MB at most).",
  not_an_image: "That is not a JPEG, PNG, or WebP image.",
  no_embedded_cover: "The EPUB has no cover of its own.",
  tracks_mismatch: "The track list changed on the server. Reload and try again.",
  chapters_mismatch: "The chapter list changed on the server. Reload and try again.",
  shelf_not_editable: "You cannot change one of those shelves.",
  shelf_cycle: "A shelf can’t go inside itself or a shelf within it.",
  too_deep: "Shelves nest at most 8 deep.",
  shelf_not_empty: "That shelf isn’t empty. Choose what happens to what’s in it.",
  parent_not_editable: "You can’t add to the shelf above this one, so its contents can’t move up.",
  smart_shelf: "Smart shelves can’t hold other shelves.",
  lookup_disabled: "Metadata suggestions are turned off. The owner can enable them in Admin.",
  rate_limited: "Too many requests. Wait a minute and try again.",
  job_running: "Alignment is running right now. Cancel it from the jobs list first.",
  no_inputs: "This book needs both an EPUB and an audiobook to align.",
  work_gone: "That book was trashed or deleted.",
  io_error: "a file could not be moved on the server",
  internal_error: "an unexpected server error",
  not_found: "it no longer exists",
  work_busy: "a read-aloud build is using it",
  invalid_server_url: "Enter the address as http://100.x.y.z:8080 (no path).",
  server_url_required: "Lectern could not find its Tailscale address. Enter it, e.g. http://100.101.102.103:8080.",
  device_revoked: "This device was revoked. Add it again to set up the plugin.",
  selection_changed: "The books in this view changed since you selected them. Select them again.",
  selection_empty: "Nothing in this view is selected any more.",
  staging_failed: "The server could not store that file. Try again later.",
  step_up_required: "Confirm it’s you to give this key more permissions.",
  step_up_failed: "That password or passkey did not confirm it’s you. Try again.",
  readers_cannot_upload: "Readers cannot let an AI app add or file books.",
  revoked: "This key was revoked.",
  busy: "The server is busy filing a book. Try again in a moment.",
};

/** Plain-language text for an API error code (the code itself when there is none). */
export function errorText(code: string): string {
  return MESSAGES[code] ?? code;
}

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
      const next = `/login?next=${encodeURIComponent(window.location.pathname)}`;
      onUnauthorized().finally(() => window.location.assign(next));
    }
    const d = data as { detail?: unknown; message?: string; errors?: { loc: string[]; msg: string }[] } | null;
    const code = typeof d?.detail === "string" ? d.detail : null;
    const field = d?.errors?.[0];
    const invalid = field ? `${field.loc.filter((x) => !/^\d+$/.test(x)).join(" ") || "Request"}: ${field.msg.replace(/^Value error, /, "")}` : null;
    const message = d?.message || invalid || (code && MESSAGES[code]) || code || `Request failed (${res.status})`;
    throw new ApiError(res.status, message, code, (data as Record<string, unknown>) ?? null, res.headers);
  }
  return data;
}

export async function get<T>(path: string): Promise<T> {
  const res = await call(path, { credentials: "same-origin", headers: { accept: "application/json" } });
  return (await parse(res)) as T;
}

async function send<T>(method: string, path: string, body?: unknown, extra?: HeadersInit, keepalive = false): Promise<T> {
  const res = await call(path, {
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
export const patch = <T>(path: string, body?: unknown, headers?: HeadersInit) => send<T>("PATCH", path, body, headers);

/** Raw binary POST (cover images); same CSRF and error handling as JSON writes. */
export async function postBlob<T>(path: string, body: Blob): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { accept: "application/json", "content-type": body.type || "application/octet-stream", "x-csrf-token": csrfToken },
    body,
  });
  return (await parse(res)) as T;
}
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

/**
 * Sign out. `wipe` deletes what the installed app keeps on this device; it runs before the
 * server is asked, so nothing is left behind even when the server cannot be reached. Then the
 * server session ends, now or (offline) on the next start or reconnect (lib/offline/signout.ts).
 */
export async function logout(wipe: (opts: { keepWorker: boolean }) => Promise<void>) {
  // Read the token before the wipe deletes the stored copy.
  markSignOut(await signOutToken(csrfToken));
  csrfToken = "";
  try {
    // Keep the app shell so an offline phone can still show that it is signing out.
    await wipe({ keepWorker: true });
  } catch {
    /* the server's Clear-Site-Data still clears it */
  }
  if (await finishSignOut()) {
    await wipe({ keepWorker: false }).catch(() => {});
    window.location.assign("/login");
  } else window.location.assign("/app");
}
