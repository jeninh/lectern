// The automatic metadata pass (Fix metadata) and library duplicate review: pure helpers the
// selection bar, the review sheet, and their tests share (lectern/autometa.py, duplicates.py).

import { display, FIELD_LABEL } from "./metadata";
import type { AutoField, FixStatus, MatchSuggestion, MetaSuggestionItem, Metadata } from "./types";

export const AUTO_FIELDS: AutoField[] = ["description", "series", "series_index", "isbn", "tags"];

const books = (n: number) => `${n} ${n === 1 ? "book" : "books"}`;

/** The toast after queueing a pass: "Looking up 3 books · 2 have nothing missing". */
export function fixSummary(results: { status: FixStatus }[] | null, counts: Partial<Record<FixStatus, number>>): { text: string; queued: number } {
  const by = (s: FixStatus) => (results ? results.filter((r) => r.status === s).length : counts[s] ?? 0);
  const queued = by("queued");
  const parts = [
    queued && `Looking up ${books(queued)}; suggestions appear for review as they arrive`,
    by("already_queued") && `${by("already_queued")} already queued`,
    by("nothing_missing") && `${books(by("nothing_missing"))} with nothing missing`,
    by("waiting_review") && `${books(by("waiting_review"))} already waiting for review`,
    by("dismissed") && `${by("dismissed")} you dismissed before (select them to ask again)`,
    by("not_found") && `${by("not_found")} not found`,
  ].filter(Boolean);
  return { text: parts.join(" · ") || "Nothing to look up", queued };
}

/** The fields a ready suggestion offers, in display order. */
export function offered(item: MetaSuggestionItem): AutoField[] {
  return AUTO_FIELDS.filter((f) => item.fields[f] !== undefined);
}

export function toggle(chosen: AutoField[], f: AutoField): AutoField[] {
  return chosen.includes(f) ? chosen.filter((x) => x !== f) : AUTO_FIELDS.filter((x) => x === f || chosen.includes(x));
}

export function fieldLabel(f: AutoField): string {
  return FIELD_LABEL[f as keyof Metadata];
}

export function fieldValue(f: AutoField, v: unknown): string {
  return display(f as keyof Metadata, v);
}

/** One line for a result that is not a suggestion to review. */
export function statusText(item: MetaSuggestionItem): string {
  if (item.status === "queued") return "Looking up…";
  if (item.status === "no_match") return "No match found for the missing fields.";
  if (item.status === "ready") return `${offered(item).length} suggestion${offered(item).length === 1 ? "" : "s"}`;
  const why: Record<string, string> = {
    lookup_offline: "Not looked up: offline mode is on.",
    lookup_disabled: "Not looked up: metadata suggestions are turned off.",
    rate_limited: "The service is busy; try again later.",
    timeout: "The service did not answer in time.",
    no_title: "Needs a title or an ISBN to look up.",
  };
  return why[item.error ?? ""] ?? "The lookup failed; try again later.";
}

/** Review wording for a possible duplicate: an upload that looks like a book you have, or
 * two books already in the library. */
export function duplicateCopy(s: Pick<MatchSuggestion, "origin">): { question: string; newLabel: string; keep: string; merge: string } {
  return s.origin === "library"
    ? { question: "Are these the same book?", newLabel: "newer", keep: "Not duplicates — ignore", merge: "Same book — merge" }
    : { question: "Is the new upload the same book?", newLabel: "new", keep: "Different books", merge: "Same book — merge" };
}

/** The Library banner: "2 uploads might belong to … · 3 possible duplicates". */
export function duplicateBanner(items: Pick<MatchSuggestion, "origin">[]): string {
  const lib = items.filter((s) => s.origin === "library").length;
  const up = items.length - lib;
  return [
    up && `${up === 1 ? "One upload" : `${up} uploads`} might belong to a book you already have`,
    lib && (lib === 1 ? "one pair of books looks like duplicates" : `${lib} pairs of books look like duplicates`),
  ]
    .filter(Boolean)
    .join("; ")
    .replace(/^o/, "O");
}
