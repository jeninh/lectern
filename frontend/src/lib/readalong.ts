// Read-along data adapter. Every server call the read-along view makes goes through this file,
// using the shapes in docs/contracts.md (tracks, sentences, progress, read-aloud EPUB) plus
// the milestone 4 spine-text route. If a route moves, only this file changes.

import { get, post, put } from "./api";
import type {
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
  return put<WorkProgressResponse>(`/api/works/${workId}/progress`, { source, device: "Web", ...at }, { keepalive });
}

export function readAloudStatus(workId: number): Promise<ReadAloudStatus> {
  return get<ReadAloudStatus>(`/api/works/${workId}/read-aloud`);
}

export function buildReadAloud(workId: number): Promise<ReadAloudStatus> {
  return post<ReadAloudStatus>(`/api/works/${workId}/read-aloud`);
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
