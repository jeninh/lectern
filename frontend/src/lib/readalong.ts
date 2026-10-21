// Read-along data adapter. Every server call the read-along view makes goes through this file,
// using the shapes in docs/contracts.md (tracks, sentences, progress, read-aloud EPUB) plus
// the milestone 4 spine-text route. If a route moves, only this file changes.

import { get, post, put } from "./api";
import type {
  AlignmentSummary,
  ReadAloudStatus,
  SentenceInfo,
  SentencesResponse,
  SpineTextResponse,
  TracksResponse,
  WorkProgressResponse,
} from "./types";

const PAGE = 2000;

export interface ReadAlongData {
  tracks: TracksResponse;
  sentences: SentenceInfo[];
  /** Spine items that contain sentences, in reading order. */
  texts: SpineTextResponse[];
  progress: WorkProgressResponse;
}

async function inBatches<T, R>(items: T[], size: number, fn: (t: T) => Promise<R>): Promise<R[]> {
  const out: R[] = [];
  for (let i = 0; i < items.length; i += size) out.push(...(await Promise.all(items.slice(i, i + size).map(fn))));
  return out;
}

export async function loadReadAlong(workId: number): Promise<ReadAlongData> {
  const base = `/api/works/${workId}`;
  const [tracks, first, progress] = await Promise.all([
    get<TracksResponse>(`${base}/tracks`),
    get<SentencesResponse>(`${base}/sentences?from=0&to=${PAGE}`),
    get<WorkProgressResponse>(`${base}/progress`),
  ]);
  const sentences = [...first.sentences];
  while (sentences.length < first.count) {
    const next = await get<SentencesResponse>(`${base}/sentences?from=${sentences.length}&to=${sentences.length + PAGE}`);
    if (!next.sentences.length) break;
    sentences.push(...next.sentences);
  }
  const spines = [...new Set(sentences.map((s) => s.spine_index))];
  const texts = await inBatches(spines, 4, (i) => get<SpineTextResponse>(`${base}/text/${i}`));
  return { tracks, sentences, texts, progress };
}

export type ProgressSource = "web" | "audio";

export function saveProgress(
  workId: number,
  source: ProgressSource,
  at: { sentence_n: number } | { audio_s: number },
  keepalive = false,
): Promise<WorkProgressResponse> {
  return put<WorkProgressResponse>(`/api/works/${workId}/progress`, { source, device: "Web", ...at }, keepalive);
}

export function readAloudStatus(workId: number): Promise<ReadAloudStatus> {
  return get<ReadAloudStatus>(`/api/works/${workId}/read-aloud`);
}

export function buildReadAloud(workId: number): Promise<ReadAloudStatus> {
  return post<ReadAloudStatus>(`/api/works/${workId}/read-aloud`);
}

export function retryAlignment(workId: number, model: string): Promise<unknown> {
  return post(`/api/works/${workId}/align`, { model });
}

// --- alignment state for the Formats card ---------------------------------------------------

export interface AlignmentHint {
  text: string;
  /** Full error text when `text` shows a shortened one. */
  detail: string | null;
  /** Heartbeat progress 0–1 of a running job, else null. */
  progress: number | null;
  docsUrl: string | null;
  /** Model the Retry button runs; null = no Retry offered. */
  retryModel: string | null;
  /** Button label: "Retry", or "Retry with base" when it steps down after running out of memory. */
  retryLabel: string | null;
}

/** `faster-whisper-small-int8` → `small` (the server's model labels). */
function modelName(label: string): string {
  return label.replace(/^faster-whisper-/, "").replace(/-(int8_float16|int8_float32|int8|float16|float32)$/, "");
}

/** "about 12 min", "about 2 h 5 min", "under a minute". */
export function aboutDuration(seconds: number): string {
  if (seconds < 60) return "under a minute";
  const m = Math.round(seconds / 60);
  if (m < 60) return `about ${m} min`;
  const h = Math.floor(m / 60);
  return `about ${h} h${m % 60 ? ` ${m % 60} min` : ""}`;
}

function shortError(error: string): string {
  const line = error.split("\n")[0].trim();
  return line.length > 140 ? `${line.slice(0, 139)}…` : line;
}

/**
 * What the alignment of a work not yet readable along is doing, from the read-aloud status:
 * real heartbeat progress and ETA, a retry waiting out its backoff, no engine to run it, or
 * a failure (with Retry). `null` when there is nothing to say about the alignment.
 */
export function alignmentHint(a: AlignmentSummary | null | undefined, now = Date.now()): AlignmentHint | null {
  if (!a) return null;
  const job = a.job ?? null;
  const base = { detail: null, progress: null, docsUrl: null, retryModel: null, retryLabel: null };
  if (job?.status === "leased") {
    const parts = ["Aligning text and audio"];
    if (job.progress != null) parts.push(`${Math.floor(job.progress * 100)}%`);
    if (job.eta_s != null) parts.push(`${aboutDuration(job.eta_s)} left`);
    if (job.attempts > 1) parts.push(`attempt ${job.attempts} of ${job.max_attempts}`);
    return { ...base, text: parts.join(" · "), progress: job.progress ?? 0 };
  }
  if (job?.status === "queued") {
    if (a.unavailable) return { ...base, text: a.unavailable.message, docsUrl: a.unavailable.docs_url };
    const wait = job.run_after ? (new Date(job.run_after).getTime() - now) / 1000 : 0;
    if (job.error && job.attempts > 0) {
      const when = wait > 0 ? `retrying in ${aboutDuration(wait).replace(/^about /, "")}` : "retrying shortly";
      return {
        ...base,
        text: `Attempt ${job.attempts} of ${job.max_attempts} failed: ${shortError(job.error)} · ${when}`,
        detail: job.error,
      };
    }
    return { ...base, text: `Waiting for the alignment worker${job.eta_s != null ? ` · done in ${aboutDuration(job.eta_s)}` : "…"}` };
  }
  if (a.status === "failed") {
    const error = job?.error || a.error;
    const m = a.retry_model ?? null;
    return {
      ...base,
      text: error ? `Alignment failed: ${shortError(error)}` : "Alignment failed",
      detail: error ?? null,
      retryModel: m,
      retryLabel: m == null ? null : m === modelName(a.model) ? "Retry" : `Retry with ${m}`,
    };
  }
  if (a.status === "pending" || a.status === "running") return { ...base, text: "Aligning text and audio…" };
  return null;
}

// --- timeline helpers -------------------------------------------------------------------

/** Index into `starts` of the last value <= t, or -1. */
export function lastAtOrBefore(starts: number[], t: number): number {
  let lo = 0;
  let hi = starts.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (starts[mid] <= t) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found;
}

/** Which track plays work-timeline second `t`, and where inside it. */
export function locate(tracks: TracksResponse["tracks"], t: number): { index: number; local: number } {
  const i = Math.max(0, lastAtOrBefore(tracks.map((x) => x.offset_s), t));
  return { index: i, local: Math.max(0, t - tracks[i].offset_s) };
}

/** Code-point offset -> UTF-16 index in `text` (sentence maps count code points). */
export function cpIndexer(text: string): (cp: number) => number {
  if (!/[\uD800-\uDFFF]/.test(text)) return (cp) => cp;
  const map: number[] = [];
  let u = 0;
  for (const ch of text) {
    map.push(u);
    u += ch.length;
  }
  map.push(u);
  return (cp) => map[Math.max(0, Math.min(cp, map.length - 1))];
}

export function formatClock(s: number): string {
  if (!Number.isFinite(s) || s < 0) s = 0;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}

/** Human duration for screen readers: "1 minute 23 seconds". */
export function spokenDuration(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  const parts = [];
  if (m) parts.push(`${m} minute${m === 1 ? "" : "s"}`);
  parts.push(`${sec} second${sec === 1 ? "" : "s"}`);
  return parts.join(" ");
}
