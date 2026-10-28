// The web reader's typography and theme (issue #79): a default per person and "Just this book"
// overrides, stored on the server (routes/api/reader.py) so they follow the person to every
// browser, with a copy in localStorage for offline use and the first paint. Only the fields
// a person changed are stored; the rest come from the default, then from DEFAULTS.

import { useCallback, useEffect, useRef, useState } from "react";
import { csrf, del, get, put } from "./api";
import { useStore } from "./store";
// Self-hosted fonts: Vite copies these into /app/assets (served by the Pi, precached by the
// service worker), so nothing is ever fetched from a font CDN. Licences: OFL-1.1, see
// frontend/fonts/README.md.
import odRegular from "@fontsource/opendyslexic/files/opendyslexic-latin-400-normal.woff2?url";
import odItalic from "@fontsource/opendyslexic/files/opendyslexic-latin-400-italic.woff2?url";
import odBold from "@fontsource/opendyslexic/files/opendyslexic-latin-700-normal.woff2?url";
import odBoldItalic from "@fontsource/opendyslexic/files/opendyslexic-latin-700-italic.woff2?url";
import ahRegular from "@fontsource/atkinson-hyperlegible/files/atkinson-hyperlegible-latin-400-normal.woff2?url";
import ahItalic from "@fontsource/atkinson-hyperlegible/files/atkinson-hyperlegible-latin-400-italic.woff2?url";
import ahBold from "@fontsource/atkinson-hyperlegible/files/atkinson-hyperlegible-latin-700-normal.woff2?url";
import ahBoldItalic from "@fontsource/atkinson-hyperlegible/files/atkinson-hyperlegible-latin-700-italic.woff2?url";
import ahExtRegular from "@fontsource/atkinson-hyperlegible/files/atkinson-hyperlegible-latin-ext-400-normal.woff2?url";
import ahExtItalic from "@fontsource/atkinson-hyperlegible/files/atkinson-hyperlegible-latin-ext-400-italic.woff2?url";
import ahExtBold from "@fontsource/atkinson-hyperlegible/files/atkinson-hyperlegible-latin-ext-700-normal.woff2?url";
import ahExtBoldItalic from "@fontsource/atkinson-hyperlegible/files/atkinson-hyperlegible-latin-ext-700-italic.woff2?url";
import lexend from "@fontsource-variable/lexend/files/lexend-latin-wght-normal.woff2?url";
import lexendExt from "@fontsource-variable/lexend/files/lexend-latin-ext-wght-normal.woff2?url";

export type ReaderTheme = "auto" | "light" | "sepia" | "dark" | "contrast";
export type ResolvedTheme = Exclude<ReaderTheme, "auto">;
export type Font = "publisher" | "serif" | "sans" | "dyslexic" | "hyperlegible" | "lexend";
export interface ReaderSettings {
  flow: "paginated" | "scrolled";
  size: number; // percent
  lineHeight: number;
  font: Font;
  width: "narrow" | "normal" | "wide";
  margin: "small" | "normal" | "large";
  theme: ReaderTheme;
  justify: boolean;
  letterSpacing: number; // em; 0 = the book's own
  wordSpacing: number; // em; 0 = the book's own
  paragraphSpacing: number; // em between paragraphs; 0 = the book's own
  heavier: boolean; // thicker strokes, for e-ink-like contrast
}
export type SettingsPatch = Partial<ReaderSettings>;
export const DEFAULTS: ReaderSettings = { flow: "paginated", size: 100, lineHeight: 1.5, font: "publisher", width: "normal", margin: "normal", theme: "auto", justify: true, letterSpacing: 0, wordSpacing: 0, paragraphSpacing: 0, heavier: false };

// What the server accepts (routes/api/reader.py): anything else from an old cache or another
// version is dropped rather than sent back and refused.
const CHOICES: { [K in keyof ReaderSettings]?: readonly ReaderSettings[K][] } = {
  flow: ["paginated", "scrolled"],
  font: ["publisher", "serif", "sans", "dyslexic", "hyperlegible", "lexend"],
  width: ["narrow", "normal", "wide"],
  margin: ["small", "normal", "large"],
  theme: ["auto", "light", "sepia", "dark", "contrast"],
};
const RANGES: { [K in keyof ReaderSettings]?: [number, number] } = {
  size: [50, 300],
  lineHeight: [1, 3],
  letterSpacing: [0, 0.3],
  wordSpacing: [0, 1],
  paragraphSpacing: [0, 3],
};

/** Only the known fields, each of the right kind and in range; `null` stays `null`. */
export function clean(s: unknown): SettingsPatch | null {
  if (!s || typeof s !== "object" || Array.isArray(s)) return null;
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(s as Record<string, unknown>)) {
    if (!(k in DEFAULTS)) continue;
    const key = k as keyof ReaderSettings;
    const choices = CHOICES[key] as readonly unknown[] | undefined;
    const range = RANGES[key];
    if (choices) {
      if (choices.includes(v)) out[k] = v;
    } else if (range) {
      if (typeof v === "number" && Number.isFinite(v)) out[k] = Math.min(range[1], Math.max(range[0], v));
    } else if (typeof v === typeof DEFAULTS[key]) out[k] = v;
  }
  return out as SettingsPatch;
}

/** What the reader shows: the book's own settings over the default over DEFAULTS. */
export function effective(def: SettingsPatch | null, book: SettingsPatch | null): ReaderSettings {
  return { ...DEFAULTS, ...clean(def), ...clean(book) };
}

export interface Stored {
  default: SettingsPatch | null;
  book: SettingsPatch | null; // null: the book follows the default
}

/**
 * Merge what the server has with this browser's copy when the reader opens. A scope with an
 * unsaved local change (`pending`, e.g. made offline) keeps the local value and is pushed.
 * The first time a person opens the reader after the update the server has no default, and
 * the old per-browser `lectern.reader` value becomes their default: it is pushed too.
 * Otherwise the server wins.
 */
export function reconcile(server: Stored, local: Stored, pending: { default: boolean; book: boolean }): { use: Stored; push: { default?: SettingsPatch; book?: SettingsPatch | null } } {
  const push: { default?: SettingsPatch; book?: SettingsPatch | null } = {};
  let def = clean(server.default);
  if (local.default && (pending.default || def === null)) {
    def = clean(local.default);
    push.default = def!;
  }
  let book = clean(server.book);
  if (pending.book) {
    book = clean(local.book);
    push.book = book;
  }
  return { use: { default: def, book }, push };
}

export const PALETTE: Record<ResolvedTheme, { bg: string; ink: string; link: string; scheme: "light" | "dark" }> = {
  light: { bg: "#fffcf6", ink: "#221d17", link: "#1f3a93", scheme: "light" },
  sepia: { bg: "#f1e5cc", ink: "#3a2e1f", link: "#6b3d12", scheme: "light" },
  dark: { bg: "#1b1712", ink: "#e6dccb", link: "#a9b8ff", scheme: "dark" },
  contrast: { bg: "#ffffff", ink: "#000000", link: "#0000c8", scheme: "light" },
};
export const THEMES: [ReaderTheme, string][] = [["auto", "Match app"], ["light", "Light"], ["sepia", "Warm"], ["dark", "Warm dark"], ["contrast", "High contrast"]];

export function resolveTheme(theme: ReaderTheme, app: string): ResolvedTheme {
  return theme === "auto" ? (app === "dark" ? "dark" : "light") : theme;
}

export const FONTS: Record<Exclude<Font, "publisher">, string> = {
  serif: `"Iowan Old Style", "Palatino Linotype", Palatino, "Newsreader", Georgia, serif`,
  sans: `"Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`,
  dyslexic: `"Lectern OpenDyslexic", "Comic Sans MS", sans-serif`,
  hyperlegible: `"Lectern Atkinson Hyperlegible", system-ui, sans-serif`,
  lexend: `"Lectern Lexend", system-ui, sans-serif`,
};
export const FONT_LABELS: [Font, string][] = [["publisher", "Book’s own"], ["serif", "Serif"], ["sans", "Sans"], ["dyslexic", "OpenDyslexic"], ["hyperlegible", "Atkinson Hyperlegible"], ["lexend", "Lexend"]];

const LATIN = "U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD";
const LATIN_EXT = "U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF";
type Face = [family: string, url: string, weight: string, style: "normal" | "italic", range: string];
const FACES: Partial<Record<Font, Face[]>> = {
  dyslexic: [
    ["Lectern OpenDyslexic", odRegular, "400", "normal", LATIN],
    ["Lectern OpenDyslexic", odItalic, "400", "italic", LATIN],
    ["Lectern OpenDyslexic", odBold, "700", "normal", LATIN],
    ["Lectern OpenDyslexic", odBoldItalic, "700", "italic", LATIN],
  ],
  hyperlegible: [
    ["Lectern Atkinson Hyperlegible", ahRegular, "400", "normal", LATIN],
    ["Lectern Atkinson Hyperlegible", ahItalic, "400", "italic", LATIN],
    ["Lectern Atkinson Hyperlegible", ahBold, "700", "normal", LATIN],
    ["Lectern Atkinson Hyperlegible", ahBoldItalic, "700", "italic", LATIN],
    ["Lectern Atkinson Hyperlegible", ahExtRegular, "400", "normal", LATIN_EXT],
    ["Lectern Atkinson Hyperlegible", ahExtItalic, "400", "italic", LATIN_EXT],
    ["Lectern Atkinson Hyperlegible", ahExtBold, "700", "normal", LATIN_EXT],
    ["Lectern Atkinson Hyperlegible", ahExtBoldItalic, "700", "italic", LATIN_EXT],
  ],
  lexend: [
    ["Lectern Lexend", lexend, "100 900", "normal", LATIN],
    ["Lectern Lexend", lexendExt, "100 900", "normal", LATIN_EXT],
  ],
};

/**
 * `@font-face` rules for the bundled fonts (all of them, or only `only`). The book's pages are
 * documents of their own (blob: URLs), where a root-relative URL does not resolve, so each
 * URL is made absolute on `origin`: this server, never anywhere else.
 */
export function fontFaces(origin: string, only?: Font): string {
  return Object.entries(FACES)
    .filter(([f]) => !only || f === only)
    .flatMap(([, faces]) => faces!)
    .map(([family, url, weight, style, range]) => `@font-face { font-family: "${family}"; src: url("${new URL(url, origin).href}") format("woff2"); font-weight: ${weight}; font-style: ${style}; font-display: swap; unicode-range: ${range}; }`)
    .join("\n");
}

/** Book CSS for the settings. */
export function bookCss(s: ReaderSettings, resolved: ResolvedTheme, origin: string): string {
  const p = PALETTE[resolved];
  const font = s.font === "publisher" ? "" : `
    ${fontFaces(origin, s.font)}
    body { font-family: ${FONTS[s.font]} !important; }
    p, li, blockquote, dd, dt, div, span, h1, h2, h3, h4, h5, h6, td, th { font-family: inherit !important; }`;
  const text = "p, li, blockquote, dd, dt, td, th, h1, h2, h3, h4, h5, h6";
  const spacing = [
    s.letterSpacing > 0 ? `letter-spacing: ${s.letterSpacing}em !important;` : "",
    s.wordSpacing > 0 ? `word-spacing: ${s.wordSpacing}em !important;` : "",
  ].join(" ").trim();
  return `
    @namespace epub "http://www.idpf.org/2007/ops";
    html { color-scheme: ${p.scheme}; font-size: ${s.size}% !important; --theme-bg-color: ${p.bg}; background: ${p.bg} !important; color: ${p.ink} !important; }
    body { background: transparent !important; color: ${p.ink} !important; }
    p, li, blockquote, dd, dt, span, div, h1, h2, h3, h4, h5, h6, td, th, em, i, b, strong, small, sup, sub { color: inherit !important; background-color: transparent !important; }
    a:link, a:visited { color: ${p.link} !important; }
    p, li, blockquote, dd { line-height: ${s.lineHeight} !important; text-align: ${s.justify ? "justify" : "start"}; hyphens: ${s.justify ? "auto" : "manual"}; widows: 2; orphans: 2; }
    [align="left"] { text-align: left; } [align="right"] { text-align: right; } [align="center"] { text-align: center; }
    pre { white-space: pre-wrap !important; }
    img, svg { max-width: 100%; height: auto; }
    ${resolved === "dark" ? "img { filter: brightness(.9); }" : ""}
    ${resolved === "contrast" ? "a:link, a:visited { text-decoration: underline !important; }" : ""}
    ${spacing ? `${text} { ${spacing} }` : ""}
    ${s.paragraphSpacing > 0 ? `p { margin-block: 0 ${s.paragraphSpacing}em !important; }` : ""}
    ${s.heavier ? "body { -webkit-text-stroke: 0.025em currentColor; }" : ""}
    ${font}
  `;
}

// --- storage --------------------------------------------------------------------------

const DEFAULT_KEY = "lectern.reader"; // the default; before #79, the only (per-browser) setting
const bookKey = (id: number) => `lectern.reader.book.${id}`;
const PENDING_KEY = "lectern.reader.pending"; // scopes with a change the server has not taken
const OWNER_KEY = "lectern.reader.user"; // whose settings these are
type Scope = "default" | number;

function readJson(key: string): unknown {
  try {
    return JSON.parse(localStorage.getItem(key) || "null");
  } catch {
    return null;
  }
}
function writeJson(key: string, v: unknown) {
  try {
    if (v === null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(v));
  } catch {
    /* quota */
  }
}
function pendingScopes(): Scope[] {
  const v = readJson(PENDING_KEY);
  return Array.isArray(v) ? v : [];
}
function markPending(scope: Scope, on: boolean) {
  const rest = pendingScopes().filter((s) => s !== scope);
  writeJson(PENDING_KEY, on ? [...rest, scope] : rest.length ? rest : null);
}

/**
 * Make this browser's reader settings `userId`'s. Someone else's (another person signed in
 * here before; clearDevice may not have run yet) are deleted first, so they are never sent
 * to this person's account. A copy with no owner, from before settings were synced, is kept:
 * it becomes this person's default (see `reconcile`).
 */
export function claimReaderStorage(userId: number) {
  const owner = readJson(OWNER_KEY);
  if (owner !== null && owner !== userId) {
    try {
      for (const k of Object.keys(localStorage)) if (/^lectern\.reader(\.|$)/.test(k)) localStorage.removeItem(k);
    } catch {
      /* no storage */
    }
  }
  writeJson(OWNER_KEY, userId);
}

function send(scope: Scope, value: SettingsPatch | null, keepalive = false): Promise<unknown> {
  if (scope === "default") return put("/api/me/reader-settings", { settings: value ?? {} }, keepalive);
  return value === null ? del(`/api/works/${scope}/reader-settings`) : put(`/api/works/${scope}/reader-settings`, { settings: value }, keepalive);
}

/**
 * The settings for one work, and the ways to change them. With `justThisBook` changes go to
 * the work's own settings, otherwise to the default. Every change is saved here at once and
 * sent to the server shortly after; one that cannot be sent (offline) is sent next time.
 */
export function useReaderSettings(workId: number) {
  const [stored, setStored] = useState<Stored>(() => ({ default: clean(readJson(DEFAULT_KEY)), book: clean(readJson(bookKey(workId))) }));
  const latest = useRef(stored);
  latest.current = stored;
  const timers = useRef(new Map<Scope, number>());
  // Writes need the session's CSRF token, which arrives with /api/me; offline there is none,
  // and changes wait in this browser (`pending`) until it is back.
  const { me } = useStore();
  const online = !!me && !!csrf();
  const userId = me?.user.id ?? null;

  const flush = useCallback((scope: Scope, keepalive = false) => {
    const t = timers.current.get(scope);
    if (t === undefined) return;
    window.clearTimeout(t);
    timers.current.delete(scope);
    if (!csrf()) return;
    const value = scope === "default" ? latest.current.default : latest.current.book;
    send(scope, value, keepalive).then(() => markPending(scope, false), () => {});
  }, []);

  useEffect(() => {
    let gone = false;
    if (userId !== null) claimReaderStorage(userId);
    const local: Stored = { default: clean(readJson(DEFAULT_KEY)), book: clean(readJson(bookKey(workId))) };
    setStored(local);
    if (!online) return;
    get<Stored>(`/api/works/${workId}/reader-settings`)
      .then((server) => {
        if (gone) return;
        const pending = pendingScopes();
        const { use, push } = reconcile(server, local, { default: pending.includes("default"), book: pending.includes(workId) });
        setStored(use);
        writeJson(DEFAULT_KEY, use.default);
        writeJson(bookKey(workId), use.book);
        if ("default" in push) send("default", push.default!).then(() => markPending("default", false), () => {});
        if ("book" in push) send(workId, push.book!).then(() => markPending(workId, false), () => {});
      })
      .catch(() => {}); // offline: this browser's copy
    const pendingTimers = timers.current;
    return () => {
      gone = true;
      for (const scope of [...pendingTimers.keys()]) flush(scope, true);
    };
  }, [workId, online, userId, flush]);

  const write = useCallback((scope: Scope, value: SettingsPatch | null) => {
    const next = scope === "default" ? { ...latest.current, default: value } : { ...latest.current, book: value };
    latest.current = next;
    setStored(next);
    writeJson(scope === "default" ? DEFAULT_KEY : bookKey(workId), value);
    markPending(scope, true);
    const t = timers.current.get(scope);
    if (t !== undefined) window.clearTimeout(t);
    timers.current.set(scope, window.setTimeout(() => flush(scope), 600));
  }, [workId, flush]);

  const justThisBook = stored.book !== null;
  return {
    settings: effective(stored.default, stored.book),
    justThisBook,
    set<K extends keyof ReaderSettings>(k: K, v: ReaderSettings[K]) {
      const cur = latest.current;
      if (cur.book !== null) write(workId, { ...cur.book, [k]: v });
      else write("default", { ...cur.default, [k]: v });
    },
    /** On: this book gets its own settings (the default's, to start with). Off: they are
     * removed and the book follows the default again. */
    setJustThisBook(on: boolean) {
      write(workId, on ? {} : null);
    },
    /** Back to the default (this book) or the app's defaults (the default). */
    reset() {
      if (latest.current.book !== null) write(workId, {});
      else write("default", {});
    },
  };
}
