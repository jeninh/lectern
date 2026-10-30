// Audiobook bookmarks and clips with notes (issue #81, lectern.audio_bookmarks): made from the
// player, shown with the KOReader highlights on the book, in the reader sheet, on the
// Highlights page, and on the read-along text. Times are work-timeline seconds; a clip is
// played back from the original tracks.

import { del, patch, post } from "./api";
import { EPUB } from "./formats";
import type { AnnotationsSummary, HighlightSearch, Work } from "./types";

export interface AudioBookmark {
  id: number;
  work_id: number;
  kind: "bookmark" | "clip";
  start_s: number;
  end_s: number | null;
  note: string | null;
  chapter: string | null;
  device: string;
  created_at: string;
  updated_at: string;
  /** With read-along alignment: the sentences spoken in it and their text in the reader. */
  sentence_n: number | null;
  sentence_end_n: number | null;
  location: { spine_index: number; char: number; length: number } | null;
  text: string | null;
}

export interface WorkAudioBookmarks {
  work_id: number;
  bookmarks: AudioBookmark[];
  export_url: string;
}

export interface AudioHit {
  work_id: number;
  title: string;
  author: string;
  bookmark: AudioBookmark;
}

/** `/api/annotations` and `/api/annotations/search` with the audio marks alongside. */
export type SummaryWithAudio = AnnotationsSummary & { audio_total?: number; works: (AnnotationsSummary["works"][number] & { audio_count?: number })[] };
export type SearchWithAudio = HighlightSearch & { audio?: AudioHit[]; audio_total?: number };

/** The longest clip the server keeps, and the shortest. */
export const MAX_CLIP_S = 30 * 60;
export const MIN_CLIP_S = 0.5;
/** The "last 30 s" shortcut. */
export const QUICK_CLIP_S = 30;

function clockOf(s: number): string {
  if (!Number.isFinite(s) || s < 0) s = 0;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}

/** "Bookmark 12:34" or "Clip 12:34–13:04". */
export function markLabel(m: Pick<AudioBookmark, "start_s" | "end_s">): string {
  return m.end_s != null ? `Clip ${clockOf(m.start_s)}–${clockOf(m.end_s)}` : `Bookmark ${clockOf(m.start_s)}`;
}

/** The clip a "last N seconds" tap makes at `t`: what was just heard, never before 0. */
export function lastSeconds(t: number, n = QUICK_CLIP_S): { start: number; end: number } {
  const end = Math.max(t, MIN_CLIP_S);
  return { start: Math.max(0, end - n), end };
}

/** Handles moved by the listener, kept valid: inside the book, start before end, and no
 * longer than a clip may be. `moved` says which handle the listener dragged, so the other
 * one gives way. */
export function clipBounds(start: number, end: number, duration: number, moved: "start" | "end" = "end"): { start: number; end: number } {
  const total = Math.max(duration, MIN_CLIP_S);
  let s = Math.max(0, Math.min(start, total - MIN_CLIP_S));
  let e = Math.max(MIN_CLIP_S, Math.min(end, total));
  if (moved === "start") {
    if (e - s < MIN_CLIP_S) e = Math.min(total, s + MIN_CLIP_S);
    if (e - s > MAX_CLIP_S) e = s + MAX_CLIP_S;
  } else {
    if (e - s < MIN_CLIP_S) s = Math.max(0, e - MIN_CLIP_S);
    if (e - s > MAX_CLIP_S) s = e - MAX_CLIP_S;
  }
  return { start: s, end: e };
}

/** Where "Open in reader" goes: the passage in the EPUB reader (aligned books only). */
export function markReaderPath(work: Pick<Work, "id" | "files">, m: AudioBookmark): string | null {
  if (!m.location || !work.files.some((f) => f.media_type === EPUB)) return null;
  const { spine_index, char, length } = m.location;
  return `/app/reader/${work.id}?at=${spine_index}:${char}&len=${length}`;
}

/** The read-along at the mark (aligned books only). */
export function markReadAlongPath(m: AudioBookmark): string | null {
  return m.sentence_n != null ? `/app/read/${m.work_id}?t=${m.start_s}` : null;
}

/** `?t=` of a read-along link: where to start instead of the saved place. */
export function startAt(search: URLSearchParams): number | null {
  const t = Number(search.get("t"));
  return search.get("t") != null && Number.isFinite(t) && t >= 0 ? t : null;
}

/** Sentence number -> the marks over it, for marking the read-along text. */
export function marksBySentence(list: AudioBookmark[]): Map<number, AudioBookmark[]> {
  const out = new Map<number, AudioBookmark[]>();
  for (const m of list) {
    if (m.sentence_n == null) continue;
    for (let n = m.sentence_n; n <= (m.sentence_end_n ?? m.sentence_n); n++) out.set(n, [...(out.get(n) ?? []), m]);
  }
  return out;
}

export const createMark = (workId: number, body: { start_s: number; end_s?: number | null; note?: string | null; chapter?: string | null; device?: string }) =>
  post<AudioBookmark>(`/api/works/${workId}/audio-bookmarks`, body);
export const updateMark = (id: number, body: { note?: string | null; start_s?: number; end_s?: number; bookmark?: boolean }) =>
  patch<AudioBookmark>(`/api/audio-bookmarks/${id}`, body);
export const deleteMark = (id: number) => del<{ deleted: boolean }>(`/api/audio-bookmarks/${id}`);

/** Tells every list of marks on the page that one was made, edited, or deleted. */
export const MARKS_CHANGED = "lectern:audio-bookmarks";
export function marksChanged(workId: number) {
  window.dispatchEvent(new CustomEvent(MARKS_CHANGED, { detail: { workId } }));
}
