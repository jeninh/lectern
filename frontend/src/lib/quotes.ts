// The quotes feed on the Highlights page and Home's highlight of the day (issue #83): your own
// highlights as cards, newest first or shuffled, filtered by book, author, shelf, and tag, with
// favourite and hide marks (routes/api/highlights.py, docs/koreader-plugin.md#quotes).

import { annotationPath } from "./annotations";
import type { Annotation, Work } from "./types";

/** An annotation as the quotes endpoints send it, with your marks. */
export type QuoteAnnotation = Annotation & { favourite: boolean; hidden: boolean };

export interface Quote {
  work_id: number;
  title: string;
  author: string;
  annotation: QuoteAnnotation;
}

/** `GET /api/quotes`. `daily`: whether Home shows a highlight of the day. */
export interface QuoteFeed {
  results: Quote[];
  total: number;
  daily: boolean;
}

/** `GET /api/quotes/today`. */
export interface QuoteOfTheDay {
  enabled: boolean;
  day: string;
  quote: Quote | null;
}

export type QuoteOrder = "newest" | "shuffle";
export type QuoteShow = "all" | "favourites" | "hidden";

export interface QuoteFilters {
  work?: string;
  author?: string;
  shelf?: string;
  tag?: string;
  order?: QuoteOrder;
  seed?: string;
  show?: QuoteShow;
}

const KEYS = ["work", "author", "shelf", "tag", "order", "seed", "show"] as const;

/** The query string for `filters`, blank ones and defaults left out (a seed only matters to a
 * shuffle). */
export function quotesQuery(filters: QuoteFilters): string {
  const p = new URLSearchParams();
  for (const k of KEYS) {
    const v = filters[k]?.trim();
    if (!v || (k === "order" && v === "newest") || (k === "show" && v === "all")) continue;
    if (k === "seed" && filters.order !== "shuffle") continue;
    p.set(k, v);
  }
  return p.toString();
}

/** Your local date, "YYYY-MM-DD": whose "today" the highlight of the day is. */
export function localDay(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** Where a quote opens: its passage in the reader, else the book's page. */
export function quotePath(q: Quote, work: Pick<Work, "id" | "files"> | undefined): string {
  return (work && annotationPath(work, q.annotation)) || `/app/library/${q.work_id}`;
}
