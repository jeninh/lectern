// Read-along on PDF pages (docs/pdf-read-along.md): the work's audio timeline drives a highlight
// drawn over pdf.js canvases. The server sends each PDF sentence as rectangles in PDF user space
// (points, origin bottom left, before /Rotate) and the work sentence it is (`work_n`); this file
// holds the pure geometry and lookups so they can be tested without a canvas.

import { get } from "./api";
import { getBook, getReadAlong, orOffline } from "./offline/store";
import { lastAtOrBefore } from "./readalong";
import type { PdfBox, PdfSentenceBoxes, PdfSentencesResponse, SentenceInfo, SentencesResponse, TracksResponse, WorkProgressResponse } from "./types";

const PAGE = 2000;

/** Every PDF sentence with its boxes, fetched in pages of 2000. */
export async function loadPdfSentences(fileId: number): Promise<PdfSentencesResponse> {
  const first = await get<PdfSentencesResponse>(`/api/files/${fileId}/sentences?from=0&to=${PAGE}`);
  const sentences = [...first.sentences];
  while (sentences.length < first.count) {
    const next = await get<PdfSentencesResponse>(`/api/files/${fileId}/sentences?from=${sentences.length}&to=${sentences.length + PAGE}`);
    if (!next.sentences.length) break;
    sentences.push(...next.sentences);
  }
  return { ...first, sentences };
}

export interface PdfReadAlongData {
  tracks: TracksResponse;
  sentences: SentenceInfo[];
  pdf: PdfSentencesResponse;
  progress: WorkProgressResponse | null;
}

/** The audio, the work's sentence timings and this PDF's boxes: from the download on this
 * device when it has them (offline), else from the server. */
export async function loadPdfReadAlong(workId: number, fileId: number): Promise<PdfReadAlongData> {
  const base = `/api/works/${workId}`;
  const [book, local] = await Promise.all([getBook(workId), getReadAlong(workId)]);
  if (book?.tracks && local?.pdf?.file_id === fileId) {
    const progress = await orOffline(() => get<WorkProgressResponse>(`${base}/progress`), async () => book.progress ?? null);
    return { tracks: book.tracks, sentences: local.sentences, pdf: local.pdf, progress };
  }
  const [tracks, first, pdf, progress] = await Promise.all([
    get<TracksResponse>(`${base}/tracks`),
    get<SentencesResponse>(`${base}/sentences?from=0&to=${PAGE}`),
    loadPdfSentences(fileId),
    get<WorkProgressResponse>(`${base}/progress`).catch(() => null),
  ]);
  const sentences = [...first.sentences];
  while (sentences.length < first.count) {
    const next = await get<SentencesResponse>(`${base}/sentences?from=${sentences.length}&to=${sentences.length + PAGE}`);
    if (!next.sentences.length) break;
    sentences.push(...next.sentences);
  }
  return { tracks, sentences, pdf, progress };
}

/** pdf.js PageViewport's affine `transform` [a, b, c, d, e, f]: user space -> CSS pixels. */
export type Affine = [number, number, number, number, number, number];

export interface Rect {
  left: number;
  top: number;
  width: number;
  height: number;
}

function apply(t: Affine, x: number, y: number): [number, number] {
  return [t[0] * x + t[2] * y + t[4], t[1] * x + t[3] * y + t[5]];
}

/** A box in PDF user space as a CSS rectangle on the rendered page (handles the flipped y axis
 * and /Rotate, both of which live in the viewport transform), padded by `pad` pixels. */
export function toRect(t: Affine, box: PdfBox, pad = 1.5): Rect {
  const [, x0, y0, x1, y1] = box;
  const [ax, ay] = apply(t, x0, y0);
  const [bx, by] = apply(t, x1, y1);
  const left = Math.min(ax, bx) - pad;
  const top = Math.min(ay, by) - pad;
  return { left, top, width: Math.abs(bx - ax) + 2 * pad, height: Math.abs(by - ay) + 2 * pad };
}

/** A point on the rendered page (CSS pixels) back in PDF user space. */
export function toPdfPoint(t: Affine, x: number, y: number): [number, number] {
  const [a, b, c, d, e, f] = t;
  const det = a * d - b * c;
  if (!det) return [0, 0];
  return [(d * (x - e) - c * (y - f)) / det, (a * (y - f) - b * (x - e)) / det];
}

/** Lookups between the audio timeline, work sentences, PDF sentences and pages. */
export interface PdfTimeline {
  /** Work sentences with audio, in timeline order. */
  matched: SentenceInfo[];
  starts: number[];
  /** work_n -> the PDF sentences that are it (usually one; a PDF may split or repeat text). */
  byWork: Map<number, PdfSentenceBoxes[]>;
  /** page (0-based) -> the PDF sentences with a box on it. */
  byPage: Map<number, PdfSentenceBoxes[]>;
}

export function buildTimeline(work: SentenceInfo[], pdf: PdfSentenceBoxes[]): PdfTimeline {
  const matched = work.filter((s) => s.matched && s.audio_start_s != null);
  const byWork = new Map<number, PdfSentenceBoxes[]>();
  const byPage = new Map<number, PdfSentenceBoxes[]>();
  for (const s of pdf) {
    if (s.work_n != null) byWork.set(s.work_n, [...(byWork.get(s.work_n) ?? []), s]);
    for (const p of new Set(s.boxes.map((b) => b[0]))) byPage.set(p, [...(byPage.get(p) ?? []), s]);
  }
  return { matched, starts: matched.map((s) => s.audio_start_s as number), byWork, byPage };
}

/** The work sentence playing at timeline second `t`, or -1 before the first. */
export function workSentenceAt(tl: PdfTimeline, t: number): number {
  const i = lastAtOrBefore(tl.starts, t);
  return i < 0 ? -1 : tl.matched[i].n;
}

/** The boxes to highlight for work sentence `n` (empty when the PDF lacks that text, e.g. an
 * EPUB-only front matter sentence: the highlight then stays off rather than guessing). */
export function boxesFor(tl: PdfTimeline, n: number): PdfBox[] {
  return (tl.byWork.get(n) ?? []).flatMap((s) => s.boxes);
}

/** The 1-based page that work sentence `n` starts on, or null when the PDF lacks it. */
export function pageOf(tl: PdfTimeline, n: number): number | null {
  const s = tl.byWork.get(n)?.[0];
  return s ? s.page + 1 : null;
}

/** Click-to-seek: the work sentence whose box on `page` (1-based) contains the PDF point,
 * else the nearest one within `slop` points (a click between lines or words). */
export function hit(tl: PdfTimeline, page: number, x: number, y: number, slop = 6): number | null {
  let best: { n: number; d: number } | null = null;
  for (const s of tl.byPage.get(page - 1) ?? []) {
    if (s.work_n == null) continue;
    for (const [p, x0, y0, x1, y1] of s.boxes) {
      if (p !== page - 1) continue;
      const dx = Math.max(x0 - x, 0, x - x1);
      const dy = Math.max(y0 - y, 0, y - y1);
      const d = Math.hypot(dx, dy);
      if (d <= slop && (!best || d < best.d)) best = { n: s.work_n, d };
    }
  }
  return best ? best.n : null;
}

/** The audio start of work sentence `n`, or of the next sentence with audio. */
export function startOf(tl: PdfTimeline, n: number): number | null {
  for (const s of tl.matched) if (s.n >= n) return s.audio_start_s;
  return null;
}
