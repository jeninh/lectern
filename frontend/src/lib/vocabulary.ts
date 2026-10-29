// The private vocabulary list (issue #80): the dictionary popover's Save button and the context
// kept with a saved word. Types live here, not in types.ts, like lib/dictionary.ts. A lookup
// stores nothing; only the explicit POST below does.

import { del, patch, post } from "./api";
import type { DictionaryEntry, LookupResult } from "./dictionary";
import { EPUB, PDF } from "./formats";

/** Where in the book a word was selected; sent only with the explicit save, never a lookup. */
export interface Place {
  work_id: number;
  file_id?: number;
  /** EPUB: the section, and the word's offset and length in its normalised text. */
  spine_index?: number;
  char?: number;
  length?: number;
  /** PDF: the 1-based page. */
  page?: number;
  /** The sentence around the word. */
  context?: string;
}

export interface SaveBody {
  word: string;
  lemma: string;
  lang: string;
  to?: string;
  definition: string;
  pos?: string;
  dictionary?: string;
  context?: string;
  work_id?: number;
  file_id?: number;
  spine_index?: number;
  char?: number;
  length?: number;
  page?: number;
}

export interface VocabularyWord {
  id: number;
  word: string;
  lemma: string | null;
  lang: string;
  to: string | null;
  definition: string;
  note: string | null;
  pos: string | null;
  dictionary: string | null;
  context: string | null;
  book: { id: number; title: string; author: string | null } | null;
  location: { spine_index: number; char: number; length: number } | null;
  page: number | null;
  file_id: number | null;
  seen_count: number;
  created_at: string;
  last_seen_at: string;
  updated_at: string;
}

export interface SaveResult {
  created: boolean;
  word: VocabularyWord;
}

const CAP = 400;
const END = /[.!?…]/;

/** The sentence around [start, end) of `text`: from after the last [.!?…] + space before it to
 * the next terminator after it (or the end of the text), cut to about 400 characters around the
 * word. Splitting is naive: "Mr. Smith" ends a sentence at "Mr." (accepted). */
export function sentenceAround(text: string, start: number, end: number): string {
  let a = 0;
  const head = text.slice(0, start);
  for (const m of head.matchAll(/[.!?…]\s+/g)) a = m.index + m[0].length;
  let b = text.length;
  for (let i = end; i < text.length; i++) {
    if (END.test(text[i]) && (i + 1 === text.length || /\s/.test(text[i + 1]))) {
      b = i + 1;
      break;
    }
  }
  if (b - a > CAP) {
    const mid = Math.floor((start + end) / 2);
    a = Math.max(a, Math.min(mid - CAP / 2, b - CAP));
    b = Math.min(b, a + CAP);
  }
  return text.slice(a, b).trim();
}

/** The body of the save POST for one entry (a definition, or a translation, which also
 * carries `to`): the lemma is the headword when the entry was found through it. */
export function saveBody(entry: DictionaryEntry, request: { query: string; place?: Place }, result: Pick<LookupResult, "lang" | "to" | "translations">): SaveBody {
  const body: SaveBody = {
    word: request.query,
    lemma: entry.lemma ? entry.headword : request.query,
    lang: entry.dictionary.lang || result.lang || "",
    definition: entry.definitions.join("\n"),
    ...request.place,
  };
  if (result.translations.includes(entry)) body.to = entry.dictionary.to || result.to || undefined;
  if (entry.pos) body.pos = entry.pos;
  if (entry.dictionary.name) body.dictionary = entry.dictionary.name;
  return body;
}

export const saveWord = (body: SaveBody) => post<SaveResult>("/api/vocabulary", body);
export const editWord = (id: number, body: { definition?: string; note?: string | null }) => patch<{ word: VocabularyWord }>(`/api/vocabulary/${id}`, body);
export const removeWord = (id: number) => del<{ removed: number }>(`/api/vocabulary/${id}`);

/** Where a saved word opens: the EPUB reader at the passage, or its PDF page with the word
 * found; null when it was saved without a place. Same shape as annotations' annotationPath. */
export function vocabularyPath(w: Pick<VocabularyWord, "word" | "book" | "location" | "page" | "file_id">, files?: { id: number; media_type: string }[]): string | null {
  if (!w.book) return null;
  if (w.location && (!files || files.some((f) => f.media_type === EPUB))) {
    const { spine_index, char, length } = w.location;
    return `/app/reader/${w.book.id}?at=${spine_index}:${char}&len=${length}`;
  }
  if (w.page && w.file_id && (!files || files.some((f) => f.id === w.file_id && f.media_type === PDF))) {
    return `/app/reader/${w.book.id}?file=${w.file_id}&page=${w.page}&find=${encodeURIComponent(w.word)}`;
  }
  return null;
}
