// Word lookup in the readers (issue #78): the server answers from offline dictionaries
// installed on it (GET /api/dictionary/lookup); nothing is asked of the internet. Answers are
// kept for the rest of this page's life, so looking a word up again is instant.

import { get } from "./api";

export interface DictionaryEntry {
  headword: string;
  pos: string | null;
  pronunciation: string | null;
  definitions: string[];
  more: number;
  query: string;
  lemma: boolean;
  dictionary: { id: string; name: string; lang: string; to: string };
}

export interface LookupResult {
  query: string;
  lang: string | null;
  to: string | null;
  installed: boolean;
  dictionaries: number;
  languages: string[];
  targets: string[];
  entries: DictionaryEntry[];
  translations: DictionaryEntry[];
}

export interface InstalledDictionary {
  id: string;
  name: string;
  lang: string;
  to: string;
  bilingual: boolean;
  entries: number;
  size_bytes: number;
  enabled: boolean;
  author: string;
  description: string;
  copyright: string;
  website: string;
  licence_files: string[];
  installed_at: string;
}

const MAX_WORDS = 6;
const MAX_CHARS = 100;

/** A selection as a lookup: trimmed, one line, at most six words; null when it is not one. */
export function cleanQuery(text: string): string | null {
  const t = text.replace(/\s+/g, " ").trim().replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "");
  if (!t || t.length > MAX_CHARS) return null;
  if (t.split(" ").length > MAX_WORDS) return null;
  if (!/\p{L}/u.test(t)) return null; // numbers and symbols are not words to look up
  return t;
}

const WORD_CHAR = /[\p{L}\p{M}\p{N}'’-]/u;

/** The word around character `index` of `text` (a tap, or a point on a PDF line). */
export function wordAt(text: string, index: number): string | null {
  const chars = Array.from(text);
  // Array.from splits by code point; map the UTF-16 index onto it.
  let i = 0;
  for (let u = 0; i < chars.length && u + chars[i].length <= index; i++) u += chars[i].length;
  if (i >= chars.length) i = chars.length - 1;
  if (i < 0 || !WORD_CHAR.test(chars[i] ?? "")) {
    if (i > 0 && WORD_CHAR.test(chars[i - 1])) i -= 1;
    else return null;
  }
  let a = i;
  let b = i;
  while (a > 0 && WORD_CHAR.test(chars[a - 1])) a--;
  while (b < chars.length - 1 && WORD_CHAR.test(chars[b + 1])) b++;
  return cleanQuery(chars.slice(a, b + 1).join(""));
}

/** The reader's own language, as the default to translate into. */
export function defaultTarget(): string {
  const l = (typeof navigator !== "undefined" && navigator.language) || "en";
  return l.split("-")[0].toLowerCase();
}

const cache = new Map<string, Promise<LookupResult>>();

/** Look a word up (cached for the session; a failed request is not cached). */
export function lookupWord(q: string, lang: string | null | undefined, to: string | null | undefined): Promise<LookupResult> {
  const params = new URLSearchParams({ q });
  if (lang) params.set("lang", lang);
  if (to) params.set("to", to);
  const key = params.toString();
  let hit = cache.get(key);
  if (!hit) {
    hit = get<LookupResult>(`/api/dictionary/lookup?${key}`);
    cache.set(key, hit);
    hit.catch(() => cache.delete(key));
    if (cache.size > 500) cache.delete(cache.keys().next().value!);
  }
  return hit;
}

/** For tests: forget the session's answers. */
export function clearLookupCache() {
  cache.clear();
}

const NAMES = typeof Intl !== "undefined" && "DisplayNames" in Intl ? new Intl.DisplayNames(undefined, { type: "language" }) : null;

/** "fr" → "French" (the code itself where the browser does not know it). */
export function languageName(code: string | null | undefined): string {
  if (!code) return "";
  try {
    return NAMES?.of(code) ?? code;
  } catch {
    return code;
  }
}

/** Where the popover goes for a word at `rect` (viewport pixels), kept inside the window. */
export function placePopover(rect: { left: number; top: number; bottom: number; width: number }, size: { w: number; h: number }, view: { w: number; h: number }, gap = 8): { left: number; top: number; below: boolean } {
  const left = Math.min(Math.max(gap, rect.left + rect.width / 2 - size.w / 2), Math.max(gap, view.w - size.w - gap));
  const below = rect.bottom + gap + size.h <= view.h - gap || rect.top - gap - size.h < gap;
  const top = below ? Math.min(rect.bottom + gap, Math.max(gap, view.h - size.h - gap)) : rect.top - gap - size.h;
  return { left, top, below };
}
