// KOReader highlights and notes as the work detail, the reader, and the Markdown export show
// them (lectern.annotations: the export uses the same order, headings, and "where" line).

import { EPUB, PDF } from "./formats";
import type { Annotation, HighlightHit, Work } from "./types";

/** "Page 12 · 2026-10-01 09:30", the line under each highlight (as in the export). */
export function annotationWhere(a: Pick<Annotation, "page" | "pageno" | "created">): string {
  const page = a.page || a.pageno;
  const when = a.created.slice(0, 16);
  return page ? `Page ${page} · ${when}` : when;
}

/** Consecutive highlights under the chapter they were made in (the list is in reading order). */
export function byChapter(list: Annotation[]): { chapter: string | null; items: Annotation[] }[] {
  const out: { chapter: string | null; items: Annotation[] }[] = [];
  let current: string | null = null;
  for (const a of list) {
    const last = out[out.length - 1];
    if (last && (!a.chapter || a.chapter === current)) last.items.push(a);
    else {
      if (a.chapter) current = a.chapter;
      out.push({ chapter: a.chapter, items: [a] });
    }
  }
  return out;
}

/** Where "Open" goes: the EPUB reader at the passage, or a PDF highlight's page in the page
 * viewer; null when neither is possible. */
export function annotationPath(work: Pick<Work, "id" | "files">, a: Annotation): string | null {
  if (a.location && work.files.some((f) => f.media_type === EPUB)) {
    const { spine_index, char, length } = a.location;
    return `/app/reader/${work.id}?at=${spine_index}:${char}&len=${length}`;
  }
  if (a.page && a.file.media_type === PDF && work.files.some((f) => f.id === a.file.id)) {
    const find = a.text.trim().split(/\s+/).slice(0, 8).join(" ");
    return `/app/reader/${work.id}?file=${a.file.id}&page=${a.page}&find=${encodeURIComponent(find)}`;
  }
  return null;
}

/** The highlights page's search and filters (`/api/annotations/search`, and the Markdown export
 * of the same results). */
export interface HighlightFilters {
  q?: string;
  work?: string;
  author?: string;
  tag?: string;
  since?: string;
  until?: string;
}

const FILTER_KEYS = ["q", "work", "author", "tag", "since", "until"] as const;

/** The query string for `filters`, blank ones left out ("" when there are none). */
export function highlightsQuery(filters: HighlightFilters): string {
  const p = new URLSearchParams();
  for (const k of FILTER_KEYS) {
    const v = filters[k]?.trim();
    if (v) p.set(k, v);
  }
  return p.toString();
}

/** Where a search hit opens: the passage in the reader, else the book's page. */
export function highlightHitPath(hit: HighlightHit, work: Pick<Work, "id" | "files"> | undefined): string {
  return (work && annotationPath(work, hit.annotation)) || `/app/library/${hit.work_id}`;
}
