// Pure helpers for the cover editor (issue #85): the 2:3 crop maths, preview sizes, the candidate
// request (debounced, stale ones aborted), radiogroup keys, and the draft each pick or revert
// produces. No React and no DOM, so all of it is unit-tested.

import type { CoverDraft } from "./editor";
import type { CoverCandidate, CoverCandidates, CoverFont, CoverHistoryEntry } from "./types";

// --- crop ----------------------------------------------------------------------------------------

/** Covers are 2:3 (width : height) everywhere they are shown: library cards, OPDS, KOReader. */
export const COVER_RATIO = 2 / 3;
/** A crop smaller than this, in pixels of the source image, would look soft on a card. */
export const MIN_COVER_WIDTH = 200;
export const MIN_COVER_HEIGHT = MIN_COVER_WIDTH / COVER_RATIO;

/** A rectangle in pixels of the source image. */
export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}
/** The same rectangle as fractions of the image (0–1), what the server's crop takes. */
export type Crop = Box;

const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);

/** The widest 2:3 box that fits the image. */
export function maxBoxWidth(iw: number, ih: number): number {
  return Math.min(iw, ih * COVER_RATIO);
}

/** The smallest box the crop control allows: 200 px wide, or the whole image if it is narrower. */
export function minBoxWidth(iw: number, ih: number): number {
  return Math.min(MIN_COVER_WIDTH, maxBoxWidth(iw, ih));
}

/** Forces a box to 2:3, no wider than the image allows and no narrower than the minimum, and
 * moves it fully inside the image. The width leads; the height follows. */
export function clampBox(b: Box, iw: number, ih: number): Box {
  const w = clamp(b.w, minBoxWidth(iw, ih), maxBoxWidth(iw, ih));
  const h = w / COVER_RATIO;
  return { x: clamp(b.x, 0, iw - w), y: clamp(b.y, 0, ih - h), w, h };
}

/** The starting crop: the largest 2:3 box, centred. */
export function initialBox(iw: number, ih: number): Box {
  const w = maxBoxWidth(iw, ih);
  const h = w / COVER_RATIO;
  return { x: (iw - w) / 2, y: (ih - h) / 2, w, h };
}

export const moveBox = (b: Box, dx: number, dy: number, iw: number, ih: number): Box => clampBox({ ...b, x: b.x + dx, y: b.y + dy }, iw, ih);

/** Changes the width around the box's centre (zoom). */
export function resizeBox(b: Box, w: number, iw: number, ih: number): Box {
  const cx = b.x + b.w / 2;
  const cy = b.y + b.h / 2;
  const nw = clamp(w, minBoxWidth(iw, ih), maxBoxWidth(iw, ih));
  const nh = nw / COVER_RATIO;
  return clampBox({ x: cx - nw / 2, y: cy - nh / 2, w: nw, h: nh }, iw, ih);
}

const round6 = (v: number) => Math.round(v * 1e6) / 1e6;

/** Pixels → fractions of the image, rounded and kept inside 0–1 so the server never sees a
 * rectangle that pokes out by a rounding error. */
export function normaliseCrop(b: Box, iw: number, ih: number): Crop {
  const x = clamp(round6(b.x / iw), 0, 1);
  const y = clamp(round6(b.y / ih), 0, 1);
  return { x, y, w: Math.min(round6(b.w / iw), round6(1 - x)), h: Math.min(round6(b.h / ih), round6(1 - y)) };
}

/** True when the crop is the whole image, so there is nothing to send. */
export const isWholeImage = (c: Crop) => c.x === 0 && c.y === 0 && c.w === 1 && c.h === 1;

/** True when the cropped area is under 200 × 300 px of the source (half a pixel of slack for rounding). */
export function cropTooSmall(c: Crop, iw: number, ih: number): boolean {
  return c.w * iw < MIN_COVER_WIDTH - 0.5 || c.h * ih < MIN_COVER_HEIGHT - 0.5;
}

/** True when not even the largest 2:3 box of this image reaches the minimum. */
export const imageTooSmall = (iw: number, ih: number) => maxBoxWidth(iw, ih) < MIN_COVER_WIDTH - 0.5;

/** The sizes the live crop previews use: a library card, and the roughly 100 px thumbnail that
 * OPDS clients and KOReader show. */
export const PREVIEW_SIZES = {
  card: { w: 160, h: 240 },
  thumb: { w: 100, h: 150 },
} as const;

/** CSS for showing the crop of an image at `width` px wide (and 3/2 as tall): a background
 * image scaled and shifted so exactly the crop shows. */
export function previewLayout(c: Crop, width: number) {
  const height = width / COVER_RATIO;
  const bgW = width / c.w;
  const bgH = height / c.h;
  return { width, height, backgroundSize: `${bgW}px ${bgH}px`, backgroundPosition: `${-c.x * bgW}px ${-c.y * bgH}px` };
}

// --- candidates ----------------------------------------------------------------------------------

export const CANDIDATE_DELAY_MS = 400;
export const CANDIDATE_PAGE = 6;
export const DEFAULT_FONT: CoverFont = "display";

export interface CandidateQuery {
  workId: number;
  title: string;
  author: string;
  font: CoverFont;
  offset: number;
  count?: number;
}

export function candidatesUrl(q: CandidateQuery): string {
  const p = new URLSearchParams({ title: q.title.trim().slice(0, 512), author: q.author.slice(0, 512), font: q.font, offset: String(q.offset), count: String(q.count ?? CANDIDATE_PAGE) });
  return `/api/works/${q.workId}/cover/candidates?${p}`;
}

export interface CandidateLoader {
  /** Ask for candidates after the debounce; a newer call replaces this one. */
  schedule(q: CandidateQuery): void;
  /** Ask now (a button press), still replacing any pending or running request. */
  now(q: CandidateQuery): void;
  cancel(): void;
}

/** Fetches candidates: waits for typing to settle, and aborts the request a newer one makes stale
 * (its late answer is dropped even if the browser delivers it). */
export function createCandidateLoader(opts: {
  onResult: (r: CoverCandidates, q: CandidateQuery) => void;
  onError: (message: string) => void;
  onBusy?: (busy: boolean) => void;
  delay?: number;
  fetcher?: typeof fetch;
}): CandidateLoader {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let ctl: AbortController | undefined;
  const fetcher = opts.fetcher ?? ((...a: Parameters<typeof fetch>) => fetch(...a));
  const stop = () => {
    clearTimeout(timer);
    ctl?.abort();
  };
  const run = async (q: CandidateQuery) => {
    const mine = new AbortController();
    ctl = mine;
    opts.onBusy?.(true);
    try {
      const res = await fetcher(candidatesUrl(q), { credentials: "same-origin", signal: mine.signal, headers: { accept: "application/json" } });
      if (!res.ok) throw new Error(res.status === 404 ? "This book is gone." : "Could not load cover designs.");
      const body = (await res.json()) as CoverCandidates;
      if (!mine.signal.aborted) opts.onResult(body, q);
    } catch (e) {
      if (!mine.signal.aborted) opts.onError((e as Error).message || "Could not load cover designs.");
    } finally {
      if (ctl === mine) opts.onBusy?.(false);
    }
  };
  return {
    schedule(q) {
      stop();
      timer = setTimeout(() => void run(q), opts.delay ?? CANDIDATE_DELAY_MS);
    },
    now(q) {
      stop();
      void run(q);
    },
    cancel() {
      stop();
      opts.onBusy?.(false);
    },
  };
}

type Design = { style?: string | null; seed?: number; font?: string };
export const sameDesign = (a: Design, b: Design) => (a.style ?? null) === (b.style ?? null) && (a.seed ?? 0) === (b.seed ?? 0) && (a.font ?? DEFAULT_FONT) === (b.font ?? DEFAULT_FONT);

/** The draft a picked candidate sets. */
export const candidateDraft = (c: CoverCandidate): CoverDraft => ({ action: "generate", style: c.style, seed: c.seed, font: c.font });

/** The candidate the draft has picked, as an index (or -1). */
export function selectedIndex(list: CoverCandidate[], draft: CoverDraft | null): number {
  return draft?.action === "generate" ? list.findIndex((c) => sameDesign(c, draft)) : -1;
}

/** Radio-group keys: arrows step (wrapping), Home and End jump. Returns the new index, or null
 * for any other key. The grid is a single group, so Up and Down step like Left and Right. */
export function radioKeyTarget(key: string, index: number, count: number): number | null {
  if (count <= 0) return null;
  if (key === "ArrowRight" || key === "ArrowDown") return (index + 1) % count;
  if (key === "ArrowLeft" || key === "ArrowUp") return (index - 1 + count) % count;
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  return null;
}

// --- history -------------------------------------------------------------------------------------

/** What reverting to a history entry saves: a generated cover by its design, an image by the
 * revision that kept it. */
export function revertDraft(e: CoverHistoryEntry): CoverDraft | null {
  if (e.current) return null;
  if (e.kind === "generated") return { action: "generate", style: e.style ?? null, seed: e.seed ?? 0, font: e.font ?? DEFAULT_FONT };
  return e.revision_id != null ? { action: "revision", revision_id: e.revision_id, preview: e.url } : null;
}
