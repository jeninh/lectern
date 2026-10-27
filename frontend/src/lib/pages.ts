// Pure page-viewer logic for PDFs and comics, kept out of the component so it can be tested.
// Pages are 1-based, as KOReader stores them.

export type Fit = "width" | "page";

/**
 * The pages on screen for `page`. In a two-page spread the first page (the cover) stands alone
 * and the rest pair up as 2–3, 4–5, …, the way a printed book or comic opens; a trailing page
 * without a partner stands alone too. Returned in reading order (the component reverses the
 * display order for right-to-left comics).
 */
export function spreadOf(page: number, total: number, spread: boolean): number[] {
  const p = clampPage(page, total);
  if (!spread || p === 1) return [p];
  const left = p % 2 === 0 ? p : p - 1;
  return left + 1 <= total ? [left, left + 1] : [left];
}

export function clampPage(page: number, total: number): number {
  if (!Number.isFinite(page)) return 1;
  return Math.max(1, Math.min(Math.max(1, total), Math.round(page)));
}

/** The first page of the next (`dir` 1) or previous (`dir` -1) screen. */
export function turn(page: number, total: number, spread: boolean, dir: 1 | -1): number {
  const shown = spreadOf(page, total, spread);
  if (dir === 1) return clampPage(shown[shown.length - 1] + 1, total);
  const prev = clampPage(shown[0] - 1, total);
  return spreadOf(prev, total, spread)[0];
}

/**
 * Scale for content `w`×`h` (one page; a spread is two pages side by side) to fit a stage
 * `sw`×`sh`, times the user's zoom. Fit width fills the width (the stage scrolls vertically);
 * fit page shows the whole page or spread.
 */
export function fitScale(fit: Fit, w: number, h: number, sw: number, sh: number, zoom = 1): number {
  if (w <= 0 || h <= 0 || sw <= 0 || sh <= 0) return zoom;
  const byWidth = sw / w;
  return (fit === "width" ? byWidth : Math.min(byWidth, sh / h)) * zoom;
}

export const ZOOMS = [0.5, 0.67, 0.8, 1, 1.25, 1.5, 2, 3, 4];

export function stepZoom(zoom: number, dir: 1 | -1): number {
  const i = ZOOMS.findIndex((z) => z >= zoom - 1e-6);
  const at = i < 0 ? ZOOMS.length - 1 : i;
  const exact = Math.abs(ZOOMS[at] - zoom) < 1e-6;
  const next = dir === 1 ? (exact ? at + 1 : at) : at - 1;
  return ZOOMS[Math.max(0, Math.min(ZOOMS.length - 1, next))];
}

export function pageLabel(shown: number[], total: number): string {
  return shown.length > 1 ? `Pages ${shown[0]}–${shown[1]} of ${total}` : `Page ${shown[0]} of ${total}`;
}

/** A piece of one pdf.js text item: UTF-16 offsets [from, to) of `items[item].str`. */
export interface TextPiece {
  item: number;
  from: number;
  to: number;
}

const fold = (s: string) => s.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();

/**
 * Where the first occurrence of `needle` is among a page's pdf.js text items (a search match
 * from the server, which read the same page with PDFium): ignoring case, accents, and
 * whitespace, since the two libraries space and split text differently.
 */
export function findInItems(items: { str: string }[], needle: string): TextPiece[] {
  const want = fold(needle).replace(/\s+/g, "");
  if (!want) return [];
  let hay = "";
  const at: [number, number, number][] = []; // per folded character: item, offset, width
  items.forEach((it, i) => {
    for (let j = 0; j < it.str.length; ) {
      const ch = String.fromCodePoint(it.str.codePointAt(j)!);
      if (!/\s/.test(ch)) {
        for (const f of fold(ch)) {
          hay += f;
          at.push([i, j, ch.length]);
        }
      }
      j += ch.length;
    }
  });
  const k = hay.indexOf(want);
  if (k < 0) return [];
  const pieces: TextPiece[] = [];
  for (const [i, j, w] of at.slice(k, k + want.length)) {
    const last = pieces[pieces.length - 1];
    if (last && last.item === i) last.to = Math.max(last.to, j + w);
    else pieces.push({ item: i, from: j, to: j + w });
  }
  return pieces;
}

/** The product of two PDF transform matrices `[a, b, c, d, e, f]` (pdf.js `Util.transform`). */
export function multiply(m: number[], n: number[]): number[] {
  return [
    m[0] * n[0] + m[2] * n[1],
    m[1] * n[0] + m[3] * n[1],
    m[0] * n[2] + m[2] * n[3],
    m[1] * n[2] + m[3] * n[3],
    m[0] * n[4] + m[2] * n[5] + m[4],
    m[1] * n[4] + m[3] * n[5] + m[5],
  ];
}
