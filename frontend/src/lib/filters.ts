// Library filters (docs/filters.md): the same grammar as lectern.filters on the server, so the
// search box and Cmd/Ctrl+K can turn typed syntax into chips as you type. The server parses
// and evaluates every filter again; this only builds and shows them. Both parsers are held to
// tests/fixtures/filters.json, and FIELDS to tests/fixtures/filter_fields.json.
//
//   format:epub,pdf -has:audio author:"Tolstoy" year:1850..1900 progress:<50% war

import { fold } from "./fuzzy";

export type Kind = "enum" | "text" | "number" | "size" | "duration" | "percent" | "date" | "shelf" | "user" | "device" | "rating";
export type Op = "=" | "<" | "<=" | ">" | ">=" | "..";

export interface Value {
  op: Op;
  a: string;
  /** The upper end of a range ("" = open). */
  b: string;
}
export interface Clause {
  key: string;
  /** Alternatives: the clause matches any of them. */
  values: Value[];
  /** "Is not". */
  negate: boolean;
}
export interface Word {
  text: string;
  negate: boolean;
}
export interface Query {
  clauses: Clause[];
  words: Word[];
}

export interface Option {
  value: string;
  label: string;
  aliases: string[];
}
export interface Field {
  key: string;
  kind: Kind;
  label: string;
  group: string;
  aliases: string[];
  options: Option[];
  /** Owners and members only (who uploaded a book). */
  members_only: boolean;
  /** Example for the editor's placeholder. */
  hint?: string;
}

const o = (value: string, label: string, ...aliases: string[]): Option => ({ value, label, aliases });
const f = (key: string, kind: Kind, label: string, group: string, extra: Partial<Field> = {}): Field => ({
  key, kind, label, group, aliases: [], options: [], members_only: false, ...extra,
});

export const FIELDS: Field[] = [
  f("format", "enum", "Format", "Files", { options: [o("epub", "EPUB"), o("pdf", "PDF"), o("cbz", "CBZ comic", "comic", "comics"), o("audiobook", "Audiobook", "audio", "m4b", "mp3")] }),
  f("kind", "enum", "Ebook or audio", "Files", { options: [o("ebook", "Ebook only", "ebook-only", "text"), o("audio", "Audio only", "audio-only"), o("both", "Ebook and audio")] }),
  f("size", "size", "File size", "Files", { hint: "100MB" }),
  f("has", "enum", "Has", "Book details", {
    options: [o("audio", "Audiobook", "audiobook"), o("cover", "Cover art", "art"), o("author", "Author", "authors"), o("series", "Series"), o("isbn", "ISBN"), o("tags", "Tags", "tag"), o("description", "Description", "summary")],
  }),
  f("audio", "enum", "Narration", "Audio & read-along", { aliases: ["narration"], options: [o("narrated", "Real narrator", "human", "real"), o("generated", "Generated", "tts", "synthesized")] }),
  f("align", "enum", "Read-along", "Audio & read-along", {
    aliases: ["alignment"],
    options: [o("aligned", "Aligned", "done"), o("running", "In progress", "in-progress", "pending", "queued"), o("failed", "Failed"), o("none", "Not aligned", "not-aligned", "no")],
  }),
  f("length", "duration", "Audio length", "Audio & read-along", { aliases: ["duration"], hint: "10h" }),
  f("narrator", "text", "Narrator", "Audio & read-along", { aliases: ["narrators", "reader"] }),
  f("status", "enum", "Reading status", "Reading", { options: [o("unread", "Not started", "not-started", "new"), o("reading", "Reading", "started"), o("finished", "Finished", "done", "read")] }),
  f("progress", "percent", "Progress", "Reading", { hint: "50%" }),
  f("read", "date", "Last read", "Reading", { aliases: ["last-read", "lastread"], hint: "30d" }),
  f("started", "date", "Started", "Reading", { hint: "2026" }),
  f("finished", "date", "Finished", "Reading", { hint: "2026-03" }),
  f("rating", "rating", "Your rating", "Reading", { aliases: ["rated", "stars"], hint: ">=4" }),
  f("title", "text", "Title", "Book details"),
  f("author", "text", "Author", "Book details", { aliases: ["by-author", "authors"] }),
  f("series", "text", "Series", "Book details"),
  f("lang", "text", "Language", "Book details", { aliases: ["language"] }),
  f("tag", "text", "Tag", "Book details", { aliases: ["tags"] }),
  f("publisher", "text", "Publisher", "Book details"),
  f("year", "number", "Year", "Book details", { aliases: ["published"], hint: "1850..1900" }),
  f("pages", "number", "Page count", "Book details", { hint: "300" }),
  f("added", "date", "Added", "Library", { hint: "30d" }),
  f("shelf", "shelf", "Shelf", "Library", { aliases: ["shelves", "in"] }),
  f("by", "user", "Uploaded by", "Library", { aliases: ["uploaded-by", "uploader"], members_only: true }),
  f("queue", "device", "Send-to-device queue", "Devices", { aliases: ["queued"] }),
  f("is", "enum", "Is", "Cleanup", { options: [o("offline", "Downloaded on this device", "downloaded"), o("duplicate", "Possible duplicate", "duplicates", "dup")] }),
  f("needs", "enum", "Needs", "Cleanup", { options: [o("ocr", "Text recognition (OCR)")] }),
];
export const GROUPS = ["Files", "Audio & read-along", "Reading", "Book details", "Library", "Devices", "Cleanup"];

/** Sort keys (lectern.filters.SORTS): label, and whether newest/largest comes first. */
export const SORTS: { key: string; label: string; desc: boolean }[] = [
  { key: "title", label: "Title", desc: false },
  { key: "author", label: "Author", desc: false },
  { key: "added", label: "Newest", desc: true },
  { key: "series", label: "Series", desc: false },
  { key: "year", label: "Year", desc: false },
  { key: "size", label: "File size", desc: true },
  { key: "length", label: "Audio length", desc: true },
  { key: "pages", label: "Page count", desc: true },
  { key: "progress", label: "Progress", desc: true },
  { key: "read", label: "Last read", desc: true },
  { key: "started", label: "Started", desc: true },
  { key: "finished", label: "Finished", desc: true },
  { key: "rating", label: "Your rating", desc: true },
  { key: "lang", label: "Language", desc: false },
  { key: "publisher", label: "Publisher", desc: false },
];

const BY_KEY = new Map<string, Field>();
for (const x of FIELDS) for (const k of [x.key, ...x.aliases]) BY_KEY.set(k, x);
export const fieldOf = (key: string): Field | undefined => BY_KEY.get(key.toLowerCase());

const COMPARABLE = new Set<Kind>(["number", "size", "duration", "percent", "date", "rating"]);
export const isComparable = (k: Kind) => COMPARABLE.has(k);
export const MAX_CHARS = 1000;
const MAX_CLAUSES = 24;
const MAX_VALUES = 20;
const MAX_WORDS = 12;
const MAX_VALUE_CHARS = 200;

export class FilterError extends Error {
  constructor(public code: string, message: string, public key: string | null = null) {
    super(message);
    this.name = "FilterError";
  }
}

const KEY_RE = /[A-Za-z][A-Za-z-]*:/y;
const OP_RE = /^(<=|>=|<|>|=)/;
const SIZE_RE = /^(\d+(?:\.\d+)?)\s*(b|k|kb|kib|m|mb|mib|g|gb|gib|t|tb|tib)?$/;
const DUR_RE = /^(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?:in)?)?(?:(\d+(?:\.\d+)?)s)?$/;
const PCT_RE = /^(\d+(?:\.\d+)?)%?$/;
const RATING_RE = /^[0-5](?:\.[05])?$/;
const DATE_RE = /^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?$/;
const AGE_RE = /^(\d+)(d|w|m|y)$/;
const isSpace = (c: string) => /\s/.test(c);

export interface Token {
  negate: boolean;
  key: string | null;
  items: [string, boolean][];
  /** Where the token starts and ends in the input (the search box keeps what is not a chip). */
  start: number;
  end: number;
}

/** Tokens: a filter token's items are its comma-separated values; a free word has one item. */
export function scan(s: string): Token[] {
  const out: Token[] = [];
  let i = 0;
  const n = s.length;
  while (i < n) {
    if (isSpace(s[i])) {
      i++;
      continue;
    }
    const start = i;
    let negate = false;
    if ((s[i] === "-" || s[i] === "!") && i + 1 < n && !isSpace(s[i + 1])) {
      negate = true;
      i++;
    }
    let key: string | null = null;
    KEY_RE.lastIndex = i;
    const m = KEY_RE.exec(s);
    if (m && fieldOf(m[0].slice(0, -1))) {
      key = fieldOf(m[0].slice(0, -1))!.key;
      i += m[0].length;
    }
    const items: [string, boolean][] = [];
    let cur = "";
    let quoted = false;
    while (i < n && !isSpace(s[i])) {
      const c = s[i];
      if (c === '"') {
        i++;
        while (i < n && s[i] !== '"') {
          if (s[i] === "\\" && i + 1 < n) i++;
          cur += s[i];
          i++;
        }
        i++;
        quoted = true;
      } else if (c === "," && key) {
        items.push([cur, quoted]);
        cur = "";
        quoted = false;
        i++;
      } else {
        cur += c;
        i++;
      }
    }
    items.push([cur, quoted]);
    out.push({ negate, key, items, start, end: Math.min(i, n) });
  }
  return out;
}

function bad(code: string, x: Field, text: string, rx: RegExp): RegExpMatchArray {
  const m = text.toLowerCase().replace(/ /g, "").match(rx);
  if (!m || !m.slice(1).some(Boolean)) throw new FilterError(code, `${x.label}: “${text}” is not a value this field understands.`, x.key);
  return m;
}

function validDate(t: string): boolean {
  const m = t.match(DATE_RE);
  if (m) {
    const y = Number(m[1]);
    const mo = Number(m[2] ?? 1);
    const d = Number(m[3] ?? 1);
    const dt = new Date(Date.UTC(y, mo - 1, d));
    if (mo >= 1 && mo <= 12 && dt.getUTCFullYear() === y && dt.getUTCMonth() === mo - 1 && dt.getUTCDate() === d) return true;
  }
  return AGE_RE.test(t);
}

function checkScalar(x: Field, text: string): string {
  const t = text.trim().toLowerCase();
  if (x.kind === "number") {
    if (!/^\d{1,9}$/.test(t)) throw new FilterError("bad_number", `${x.label}: “${text}” is not a whole number.`, x.key);
  } else if (x.kind === "rating") {
    if (!RATING_RE.test(t) || Number(t) > 5) throw new FilterError("bad_rating", `${x.label}: “${text}” is not 0 to 5 stars in halves (4.5).`, x.key);
  } else if (x.kind === "size") bad("bad_size", x, t, SIZE_RE);
  else if (x.kind === "duration") {
    if (!/^\d+(?:\.\d+)?$/.test(t)) bad("bad_duration", x, t, DUR_RE);
  } else if (x.kind === "percent") {
    const m = bad("bad_percent", x, t, PCT_RE);
    if (Number(m[1]) > 100) throw new FilterError("bad_percent", `${x.label}: at most 100%.`, x.key);
  } else if (x.kind === "date") {
    if (!validDate(t)) throw new FilterError("bad_date", `${x.label}: “${text}” is not a date (2026, 2026-03, 2026-03-15) or an age (30d, 2w, 6m, 1y).`, x.key);
  }
  return t;
}

function toValue(x: Field, text: string, quoted: boolean): Value {
  if (isComparable(x.kind) && !quoted) {
    let t = text.trim();
    const m = t.match(OP_RE);
    if (m && m[1] !== "=") return { op: m[1] as Op, a: checkScalar(x, t.slice(m[1].length)), b: "" };
    if (m) t = t.slice(1);
    if (t.includes("..")) {
      const at = t.indexOf("..");
      const a = t.slice(0, at);
      const b = t.slice(at + 2);
      if (!a.trim() && !b.trim()) throw new FilterError("empty_value", `${x.label}: a range needs at least one end.`, x.key);
      if (x.kind === "date" && a.trim() && b.trim() && AGE_RE.test(a.trim().toLowerCase()) !== AGE_RE.test(b.trim().toLowerCase()))
        throw new FilterError("bad_date", `${x.label}: a range is two dates or two ages.`, x.key);
      return { op: "..", a: a.trim() ? checkScalar(x, a) : "", b: b.trim() ? checkScalar(x, b) : "" };
    }
    return { op: "=", a: checkScalar(x, t), b: "" };
  }
  if (isComparable(x.kind)) return { op: "=", a: checkScalar(x, text), b: "" };
  const t = text.trim();
  if (x.kind === "enum") {
    const low = t.toLowerCase();
    const hit = x.options.find((op) => op.value === low || op.aliases.includes(low));
    if (!hit) throw new FilterError("unknown_value", `${x.label}: “${t}” is not one of ${x.options.map((op) => op.value).join(", ")}.`, x.key);
    return { op: "=", a: hit.value, b: "" };
  }
  if (t.length > MAX_VALUE_CHARS) throw new FilterError("too_long", `${x.label}: a value is at most ${MAX_VALUE_CHARS} characters.`, x.key);
  return { op: "=", a: t, b: "" };
}

const sameValue = (p: Value, q: Value) => p.op === q.op && p.a === q.a && p.b === q.b;

/** One filter token as a clause (throws FilterError for a value its field cannot take). */
export function tokenClause(t: Token): Clause {
  const x = FIELDS.find((y) => y.key === t.key)!;
  const items = t.items.filter(([s, q]) => s.trim() || q);
  if (!items.length) throw new FilterError("empty_value", `${x.label}: add a value after “${x.key}:”.`, x.key);
  if (items.length > MAX_VALUES) throw new FilterError("too_many", `${x.label}: at most ${MAX_VALUES} values.`, x.key);
  const values: Value[] = [];
  for (const [s, q] of items) {
    const v = toValue(x, s, q);
    if (!values.some((w) => sameValue(w, v))) values.push(v);
  }
  return { key: x.key, values, negate: t.negate };
}

export function parse(text: string): Query {
  if (text.length > MAX_CHARS) throw new FilterError("too_long", `A filter is at most ${MAX_CHARS} characters.`);
  const q: Query = { clauses: [], words: [] };
  for (const t of scan(text)) {
    if (t.key === null) {
      const word = t.items[0][0].trim();
      if (word) q.words.push({ text: word, negate: t.negate });
      continue;
    }
    q.clauses.push(tokenClause(t));
  }
  if (q.clauses.length > MAX_CLAUSES) throw new FilterError("too_many", `A filter has at most ${MAX_CLAUSES} conditions.`);
  if (q.words.length > MAX_WORDS) throw new FilterError("too_many", `A filter has at most ${MAX_WORDS} words.`);
  return q;
}

const quote = (t: string) => `"${t.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;

function formatValue(x: Field, v: Value): string {
  if (v.op === "..") return `${v.a}..${v.b}`;
  if (v.op !== "=") return `${v.op}${v.a}`;
  if (x.kind === "enum" || isComparable(x.kind)) return v.a;
  if (!v.a || /[\s,"]/.test(v.a) || OP_RE.test(v.a) || v.a.includes("..")) return quote(v.a);
  return v.a;
}

export function formatClause(c: Clause): string {
  const x = FIELDS.find((y) => y.key === c.key)!;
  return `${c.negate ? "-" : ""}${c.key}:${c.values.map((v) => formatValue(x, v)).join(",")}`;
}

export function formatWord(w: Word): string {
  let t = w.text;
  if (/[\s",:]/.test(t) || t[0] === "-" || t[0] === "!") t = quote(t);
  return `${w.negate ? "-" : ""}${t}`;
}

export function formatQuery(q: Query): string {
  return [...q.clauses.map(formatClause), ...q.words.map(formatWord)].join(" ");
}

/** The clauses of a canonical filter (the URL's `f`, a smart shelf's rule); [] if unreadable. */
export function clausesOf(f: string): Clause[] {
  try {
    return parse(f).clauses;
  } catch {
    return [];
  }
}

/**
 * What the search box keeps and what becomes chips. A filter token becomes a chip once it is
 * finished: followed by a space, or everything when `all` (Enter). A token that is still being
 * typed, or that has a value its field cannot take, stays in the box (`error` says why).
 */
export function extractChips(text: string, all: boolean): { clauses: Clause[]; rest: string; error: FilterError | null } {
  const clauses: Clause[] = [];
  let error: FilterError | null = null;
  let rest = "";
  let last = 0;
  for (const t of scan(text)) {
    const done = all || t.end < text.length;
    if (t.key === null || !done) continue;
    try {
      clauses.push(tokenClause(t));
      rest += text.slice(last, t.start);
      last = t.end;
    } catch (e) {
      if (e instanceof FilterError) error = e;
      else throw e;
    }
  }
  rest += text.slice(last);
  return { clauses, rest: rest.replace(/\s{2,}/g, " ").replace(/^\s+/, ""), error };
}

// --- labels ------------------------------------------------------------------------------------

const OP_LABEL: Record<Exclude<Op, "..">, string> = { "=": "", "<": "under ", "<=": "at most ", ">": "over ", ">=": "at least " };
const DATE_OP: Record<Exclude<Op, "..">, string> = { "=": "", "<": "before ", "<=": "up to ", ">": "after ", ">=": "from " };

function ageLabel(t: string): string | null {
  const m = t.match(AGE_RE);
  if (!m) return null;
  const n = Number(m[1]);
  const unit = { d: "day", w: "week", m: "month", y: "year" }[m[2] as "d" | "w" | "m" | "y"];
  return `${n} ${unit}${n === 1 ? "" : "s"}`;
}

function scalarLabel(x: Field, t: string): string {
  if (x.kind === "percent") return t.endsWith("%") ? t : `${t}%`;
  if (x.kind === "rating") return `${t} ★`;
  if (x.kind === "size") {
    const m = t.replace(/ /g, "").match(SIZE_RE);
    if (!m) return t;
    const u = (m[2] ?? "mb").toUpperCase();
    return `${m[1]} ${u === "B" ? "bytes" : u.length === 1 ? `${u}B` : u.replace("IB", "iB")}`;
  }
  if (x.kind === "duration") return /^\d+(?:\.\d+)?$/.test(t) ? `${t} h` : t.replace(/(\d)([hms])/g, "$1 $2 ").trim();
  return t;
}

/** Display names for values that are references: a shelf or device id ("#12"). */
export type Names = (key: "shelf" | "queue", ref: string) => string | undefined;

/** A value in words: "EPUB", "under 50%", "1850–1900", "in the last 30 days". */
export function valueLabel(x: Field, v: Value, names?: Names): string {
  if (x.kind === "enum") return x.options.find((op) => op.value === v.a)?.label ?? v.a;
  if (x.kind === "shelf") {
    if (v.a.toLowerCase() === "none") return "not on a shelf";
    if (v.a.toLowerCase() === "any") return "on any shelf";
    return names?.("shelf", v.a) ?? (v.a.startsWith("#") ? "a deleted shelf" : v.a);
  }
  if (x.kind === "device") {
    if (v.a.toLowerCase() === "any") return "any device";
    return names?.("queue", v.a) ?? (v.a.startsWith("#") ? "a removed device" : v.a);
  }
  if (x.kind === "date") {
    const age = (t: string) => ageLabel(t);
    if (v.op === "..") {
      if (age(v.a) || age(v.b)) return `${v.a ? `${age(v.a)} ago` : "now"} to ${v.b ? `${age(v.b)} ago` : "any time"}`.replace(/^(.*) ago to (.*) ago$/, "$1 to $2 ago");
      return v.a && v.b ? `${v.a} to ${v.b}` : v.a ? `from ${v.a}` : `up to ${v.b}`;
    }
    const a = age(v.a);
    if (a) return v.op === ">" || v.op === ">=" ? `more than ${a} ago` : `in the last ${a}`;
    return `${DATE_OP[v.op]}${v.a}`;
  }
  if (isComparable(x.kind)) {
    if (v.op === "..") return v.a && v.b ? `${scalarLabel(x, v.a)}–${scalarLabel(x, v.b)}` : v.a ? `at least ${scalarLabel(x, v.a)}` : `at most ${scalarLabel(x, v.b)}`;
    return `${OP_LABEL[v.op]}${scalarLabel(x, v.a)}`;
  }
  return v.a;
}

/** A chip's text: "Format: EPUB or PDF", "Has audiobook: no" for -has:audio. */
export function clauseLabel(c: Clause, names?: Names): { field: string; values: string } {
  const x = FIELDS.find((y) => y.key === c.key)!;
  const values = c.values.map((v) => valueLabel(x, v, names));
  if (x.key === "has" || x.key === "is" || x.key === "needs") {
    const what = values.join(" or ");
    const field = { has: `Has ${what.toLowerCase()}`, is: what, needs: `Needs ${what.toLowerCase().replace("text recognition (ocr)", "OCR")}` }[x.key]!;
    return { field, values: c.negate ? "no" : "yes" };
  }
  return { field: x.label, values: `${c.negate ? "is not " : ""}${values.join(" or ")}` };
}

/** Same text as the server's fold, for matching shelf paths typed as values. */
export const foldPath = (s: string) => fold(s.trim().replace(/^\/+|\/+$/g, ""));
