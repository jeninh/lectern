// Client-side mirror of the server's metadata rules (lectern/workmeta.py) so forms say what to
// fix before saving, plus the small pure helpers the review and editor screens share.

import type { AuthorName, Metadata } from "./types";

export const LANG_RE = /^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$/;
export const DATE_RE = /^\d{4}(-\d{2}(-\d{2})?)?$/;

/** ISBN-10 or ISBN-13 with a valid check digit; returns the bare digits, or null. */
export function normalizeIsbn(raw: string): string | null {
  const s = raw.replace(/[\s-]/g, "").toUpperCase();
  if (/^\d{9}[\dX]$/.test(s)) {
    const sum = [...s].reduce((acc, c, i) => acc + (c === "X" ? 10 : Number(c)) * (10 - i), 0);
    return sum % 11 === 0 ? s : null;
  }
  if (/^97[89]\d{10}$/.test(s)) {
    const sum = [...s.slice(0, 12)].reduce((acc, c, i) => acc + Number(c) * (i % 2 ? 3 : 1), 0);
    return (10 - (sum % 10)) % 10 === Number(s[12]) ? s : null;
  }
  return null;
}

export type FieldErrors = Partial<Record<keyof Metadata, string>>;

/** XML 1.0-illegal control characters (tab, newline, and carriage return are fine). */
// eslint-disable-next-line no-control-regex
export const CONTROL_RE = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/;

export function validate(m: Metadata): FieldErrors {
  const e: FieldErrors = {};
  if (!m.title.trim()) e.title = "A title is required.";
  if (m.authors.some((a) => !a.name.trim())) e.authors = "Every author needs a name (remove empty rows).";
  if (m.language && !LANG_RE.test(m.language.trim())) e.language = "Use a language code such as en or pt-BR.";
  if (m.isbn && !normalizeIsbn(m.isbn)) e.isbn = "Not a valid ISBN-10 or ISBN-13 (check the digits).";
  if (m.published && !DATE_RE.test(m.published.trim())) e.published = "Use YYYY, YYYY-MM, or YYYY-MM-DD.";
  if (m.series_index != null && (Number.isNaN(m.series_index) || m.series_index < 0)) e.series_index = "Use a number such as 1 or 2.5.";
  // The server refuses characters XML cannot carry (type ``control_characters``); say so here.
  for (const [k, v] of Object.entries(m) as [keyof Metadata, unknown][]) {
    const texts = Array.isArray(v) ? v.map((x) => (typeof x === "string" ? x : `${x?.name ?? ""}${x?.sort ?? ""}`)) : [v];
    if (!e[k] && texts.some((t) => typeof t === "string" && CONTROL_RE.test(t))) e[k] = "Remove invisible control characters (often pasted from a PDF).";
  }
  return e;
}

/** "Ursula K. Le Guin" → "Le Guin, Ursula K."; keeps particles with the surname. */
export function sortName(name: string): string {
  const parts = name.trim().split(/\s+/);
  if (parts.length < 2 || name.includes(",")) return name.trim();
  let i = parts.length - 1;
  while (i > 1 && /^(de|da|del|der|di|du|la|le|van|von|ten|ter)$/i.test(parts[i - 1])) i--;
  return `${parts.slice(i).join(" ")}, ${parts.slice(0, i).join(" ")}`;
}

/** Strips blanks so equal forms compare equal and the server gets clean input. */
export function clean(m: Metadata): Metadata {
  const t = (v: string | null) => (v && v.trim() ? v.trim() : null);
  return {
    title: m.title.trim(),
    sort_title: t(m.sort_title),
    authors: m.authors.filter((a) => a.name.trim()).map((a): AuthorName => ({ name: a.name.trim(), sort: t(a.sort ?? null) })),
    series: t(m.series),
    series_index: t(m.series) ? m.series_index : null,
    description: m.description && m.description.trim() ? m.description : null,
    language: t(m.language),
    isbn: m.isbn ? normalizeIsbn(m.isbn) ?? m.isbn.trim() : null,
    publisher: t(m.publisher),
    published: t(m.published),
    tags: dedupe(m.tags),
    narrators: dedupe(m.narrators),
  };
}

export function dedupe(list: string[]): string[] {
  const seen = new Map<string, string>();
  for (const x of list.map((s) => s.trim()).filter(Boolean)) if (!seen.has(x.toLowerCase())) seen.set(x.toLowerCase(), x);
  return [...seen.values()];
}

export const FIELD_LABEL: Record<keyof Metadata, string> = {
  title: "Title",
  sort_title: "Sort title",
  authors: "Authors",
  series: "Series",
  series_index: "Number in series",
  description: "Description",
  language: "Language",
  isbn: "ISBN",
  publisher: "Publisher",
  published: "Published",
  tags: "Tags",
  narrators: "Narrators",
};

export function display(field: keyof Metadata, v: unknown): string {
  if (v == null || v === "") return "—";
  if (field === "authors") return (v as AuthorName[]).map((a) => a.name).join(", ") || "—";
  if (Array.isArray(v)) return v.join(", ") || "—";
  return String(v);
}

const same = (a: unknown, b: unknown) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

/** Fields a suggestion would change, in display order; equal or empty suggestions are skipped. */
export function suggestionDiff(current: Metadata, fields: Partial<Metadata>): (keyof Metadata)[] {
  return (Object.keys(FIELD_LABEL) as (keyof Metadata)[]).filter((k) => {
    const v = fields[k];
    if (v == null || (Array.isArray(v) && v.length === 0) || v === "") return false;
    if (k === "authors") return !same((v as AuthorName[]).map((a) => a.name), current.authors.map((a) => a.name));
    return !same(v, current[k]);
  });
}

/** Applies only the chosen fields; everything else stays exactly as the person left it. */
export function applyFields(current: Metadata, fields: Partial<Metadata>, chosen: (keyof Metadata)[]): Metadata {
  const next = { ...current };
  for (const k of chosen) {
    const v = fields[k];
    if (v === undefined) continue;
    if (k === "authors") next.authors = (v as AuthorName[]).map((a) => ({ name: a.name, sort: a.sort ?? sortName(a.name) }));
    else (next as Record<string, unknown>)[k] = Array.isArray(v) ? [...v] : v;
  }
  return next;
}

/** Field names whose value differs; used for the unsaved-changes summary and conflicts. */
export function changedFields(a: Metadata, b: Metadata): (keyof Metadata)[] {
  return (Object.keys(FIELD_LABEL) as (keyof Metadata)[]).filter((k) => !same(a[k], b[k]));
}
