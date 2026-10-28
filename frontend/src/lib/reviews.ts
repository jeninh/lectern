// Your private rating, notes, and read-throughs of a book (lectern.reviews, docs/reviews.md).
// The server checks everything again; these only shape what the detail page shows and sends.
import MarkdownIt from "markdown-it";
import type { Token } from "markdown-it";
import type { ReadThrough } from "./types";

/** "4½ stars", "1 star", "No rating". */
export function starsLabel(rating: number | null): string {
  if (!rating) return "No rating";
  const whole = Math.floor(rating);
  const half = rating - whole === 0.5;
  const n = half ? (whole ? `${whole}½` : "½") : String(whole);
  return `${n} ${rating === 1 ? "star" : "stars"}`;
}

/** How full star `i` (1–5) is for `rating`: 0, 0.5, or 1. */
export function starFill(rating: number | null, i: number): 0 | 0.5 | 1 {
  const r = rating ?? 0;
  return r >= i ? 1 : r >= i - 0.5 ? 0.5 : 0;
}

/** A read-through being edited: dates as `<input type="date">` gives them ("" = unknown). */
export interface DraftRead {
  started_on: string;
  finished_on: string;
}

export const toDraft = (r: ReadThrough): DraftRead => ({ started_on: r.started_on ?? "", finished_on: r.finished_on ?? "" });

/** What the editor sends, or a sentence saying what is wrong (the server's checks, early). */
export function readsBody(drafts: DraftRead[], today: string): { started_on: string | null; finished_on: string | null }[] | string {
  const out = [];
  for (const d of drafts) {
    const s = d.started_on || null;
    const f = d.finished_on || null;
    if (!s && !f) return "Each read needs a start or finish date (or remove it).";
    if (s && f && f < s) return "A read cannot finish before it starts.";
    if ((s && s > today) || (f && f > today)) return "Dates cannot be in the future.";
    out.push({ started_on: s, finished_on: f });
  }
  return out;
}

/** Today in the browser's time zone, as YYYY-MM-DD. */
export function localToday(now = new Date()): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}`;
}

/** "Mar 15, 2026" for a YYYY-MM-DD day (no time zone shift), "?" when unknown. */
export function dayLabel(day: string | null): string {
  if (!day) return "?";
  const [y, m, d] = day.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
}

const md = new MarkdownIt({ html: false, linkify: false, typographer: false });

/** Notes as markdown-it tokens for DocMarkdown (no HTML, so nothing in them becomes markup).
 * A heading becomes a bold paragraph, so "# Thoughts" sits inside the section instead of
 * competing with the page's headings or getting the docs' anchor links. */
export function notesTokens(notes: string): Token[] {
  const tokens = md.parse(notes, {});
  tokens.forEach((t, i) => {
    if (t.type === "heading_open" || t.type === "heading_close") {
      t.type = t.type === "heading_open" ? "paragraph_open" : "paragraph_close";
      t.tag = "p";
      const inline = tokens[i + 1];
      if (t.nesting === 1 && inline?.type === "inline") {
        const Tok = t.constructor as new (type: string, tag: string, nesting: -1 | 0 | 1) => Token;
        inline.children = [new Tok("strong_open", "strong", 1), ...(inline.children ?? []), new Tok("strong_close", "strong", -1)];
      }
    }
  });
  return tokens;
}
