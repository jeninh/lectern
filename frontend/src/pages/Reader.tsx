// In-browser EPUB reader on foliate-js: paginated or scrolled, typography and theme controls,
// table of contents, and canonical progress (spine index + normalised character offset, the
// same coordinates KOReader and the audio player map to) with a hand-off prompt instead of
// silent jumps.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, Chevron, TextSize, Toc } from "../components/Icons";
import { Sheet } from "../components/Sheet";
import { ApiError, get, OfflineError } from "../lib/api";
import { getBook, orOffline } from "../lib/offline/store";
import { savePosition } from "../lib/offline/sync";
import { relativeTime } from "../lib/format";
import { pickHandoff } from "../lib/handoff";
import { useLocalStorage } from "../lib/hooks";
import { useStore } from "../lib/store";
import { hitTarget } from "../lib/formats";
import { offsetOf, rangeAt, rangeBetween } from "../lib/textpos";
import type { HandoffCandidate, Work, WorkProgressResponse } from "../lib/types";
import { PageReader } from "./PageReader";

type ReaderTheme = "auto" | "light" | "sepia" | "dark";
type Font = "publisher" | "serif" | "sans";
export interface ReaderSettings {
  flow: "paginated" | "scrolled";
  size: number; // percent
  lineHeight: number;
  font: Font;
  width: "narrow" | "normal" | "wide";
  margin: "small" | "normal" | "large";
  theme: ReaderTheme;
  justify: boolean;
}
const DEFAULTS: ReaderSettings = { flow: "paginated", size: 100, lineHeight: 1.5, font: "publisher", width: "normal", margin: "normal", theme: "auto", justify: true };

const PALETTE: Record<Exclude<ReaderTheme, "auto">, { bg: string; ink: string; link: string; scheme: "light" | "dark" }> = {
  light: { bg: "#fffcf6", ink: "#221d17", link: "#1f3a93", scheme: "light" },
  sepia: { bg: "#f1e5cc", ink: "#3a2e1f", link: "#6b3d12", scheme: "light" },
  dark: { bg: "#1b1712", ink: "#e6dccb", link: "#a9b8ff", scheme: "dark" },
};
const FONTS: Record<Exclude<Font, "publisher">, string> = {
  serif: `"Iowan Old Style", "Palatino Linotype", Palatino, "Newsreader", Georgia, serif`,
  sans: `"Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`,
};
const WIDTH = { narrow: 560, normal: 680, wide: 860 };
const GAP = { small: "4%", normal: "7%", large: "11%" };

/** Book CSS for the settings (exported for tests). */
export function bookCss(s: ReaderSettings, resolved: Exclude<ReaderTheme, "auto">): string {
  const p = PALETTE[resolved];
  const font = s.font === "publisher" ? "" : `
    body { font-family: ${FONTS[s.font]} !important; }
    p, li, blockquote, dd, dt, div, span, h1, h2, h3, h4, h5, h6, td, th { font-family: inherit !important; }`;
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
    ${font}
  `;
}

const FOUND = "lectern-found";

/** Mark a search match in a section's document (the CSS Custom Highlight API where the
 * browser has it, else the selection), and return its range for the reader to show. */
function highlight(doc: Document, start: number, end: number): Range | null {
  const range = rangeBetween(doc, start, end);
  if (!range) return null;
  const win = doc.defaultView as (Window & { Highlight?: new (...r: Range[]) => unknown; CSS?: { highlights?: Map<string, unknown> } }) | null;
  if (win?.Highlight && win.CSS?.highlights) {
    // Its own rule, not the book CSS: the reader injects that only into documents with a <head>.
    if (!doc.getElementById(FOUND)) {
      const style = doc.createElementNS("http://www.w3.org/1999/xhtml", "style");
      style.id = FOUND;
      style.textContent = `::highlight(${FOUND}) { background-color: rgb(255 196 0 / 0.42); }`;
      (doc.head ?? doc.documentElement).append(style);
    }
    win.CSS.highlights.set(FOUND, new win.Highlight(range));
  }
  else {
    const sel = doc.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(range);
  }
  return range;
}

interface TocItem {
  label: string;
  href: string;
  subitems?: TocItem[] | null;
}
interface FoliateView extends HTMLElement {
  open(book: File): Promise<void>;
  init(o: { lastLocation?: string; showTextStart?: boolean }): Promise<void>;
  goTo(target: string | number): Promise<unknown>;
  goToFraction(f: number): Promise<void>;
  goLeft(): Promise<void>;
  goRight(): Promise<void>;
  close(): void;
  book: { toc?: TocItem[]; dir?: string; metadata?: { title?: string } };
  renderer: HTMLElement & { setStyles(css: string): void; goTo(t: { index: number; anchor?: (doc: Document) => Range | Element | null }): Promise<void>; next(): Promise<void>; prev(): Promise<void> };
}
interface Relocate {
  fraction: number;
  section?: { current: number; total: number };
  tocItem?: { label?: string } | null;
  cfi: string;
  range: Range;
}

interface ReadMark {
  cfi: string;
  seenAt: string | null;
}
const markKey = (id: number) => `lectern.read.${id}`;
function readMark(id: number): ReadMark | null {
  try {
    return JSON.parse(localStorage.getItem(markKey(id)) || "null");
  } catch {
    return null;
  }
}
function writeMark(id: number, m: ReadMark) {
  try {
    localStorage.setItem(markKey(id), JSON.stringify(m));
  } catch {
    /* quota */
  }
}

const EMPTY_PROGRESS = (id: number): WorkProgressResponse => ({ work_id: id, position: null, source: null, raw: null, device: null, received_at: null, text: null, audio_s: null, percent: null, estimated: false, count: 0, total_duration_s: 0, koreader: null });

/** `/app/reader/:id` opens the EPUB reader; `?file=<id>` opens a PDF or comic in the page viewer. */
export function ReaderPage() {
  const { id: idParam } = useParams();
  const [search] = useSearchParams();
  const fileId = Number(search.get("file"));
  if (Number.isFinite(fileId) && fileId > 0) return <PageReader workId={Number(idParam)} fileId={fileId} />;
  return <EpubReader />;
}

function EpubReader() {
  const { id: idParam } = useParams();
  const id = Number(idParam);
  // Opened from a search hit ("In books" in Cmd/Ctrl+K): start at the match, highlighted.
  const [search] = useSearchParams();
  const target = useRef(hitTarget(search));
  const atKey = `${search.get("at") ?? ""}|${search.get("len") ?? ""}`;
  const lastAt = useRef(atKey);
  const nav = useNavigate();
  const { works, resolvedTheme, deviceName, toast, now } = useStore();
  const [settings, setSettings] = useLocalStorage<ReaderSettings>("lectern.reader", DEFAULTS);
  const s = { ...DEFAULTS, ...settings };
  const set = <K extends keyof ReaderSettings>(k: K, v: ReaderSettings[K]) => setSettings({ ...s, [k]: v });
  const theme: Exclude<ReaderTheme, "auto"> = s.theme === "auto" ? (resolvedTheme === "dark" ? "dark" : "light") : s.theme;

  const [work, setWork] = useState<Work | null>(() => works.find((w) => w.id === id) ?? null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loc, setLoc] = useState<{ fraction: number; chapter: string } | null>(null);
  const [toc, setToc] = useState<TocItem[]>([]);
  const [panel, setPanel] = useState<"toc" | "type" | null>(null);
  const [handoff, setHandoff] = useState<HandoffCandidate | null>(null);
  const [chrome, setChrome] = useState(true);
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<FoliateView | null>(null);
  const ready = useRef(false);
  const saveTimer = useRef<number | null>(null);
  const lastSaved = useRef<string>("");
  const pending = useRef<{ spine_index: number; char: number; raw: string } | null>(null);
  // Only a position the reader moved to is saved: opening a book, or leaving it untouched,
  // must never overwrite a newer position from another device.
  const dirty = useRef(false);

  useEffect(() => {
    if (!work || work.id !== id) orOffline(() => get<Work>(`/api/works/${id}`), async () => (await getBook(id))?.work).then(setWork).catch((e) => setError((e as Error).message));
  }, [id, work]);

  const save = useCallback(
    async (keepalive = false) => {
      const p = pending.current;
      if (!p || !dirty.current) return;
      const key = `${p.spine_index}:${p.char}`;
      if (key === lastSaved.current) return;
      lastSaved.current = key;
      dirty.current = false;
      try {
        // Offline, the position waits in the queue and syncs on reconnect (lib/offline/sync.ts).
        const r = await savePosition<WorkProgressResponse>({ kind: "work", id, workId: id, title: work?.title ?? "this book" }, { source: "web", device: deviceName, ...p }, keepalive);
        writeMark(id, { cfi: p.raw, seenAt: r?.received_at ?? readMark(id)?.seenAt ?? null });
      } catch {
        writeMark(id, { cfi: p.raw, seenAt: readMark(id)?.seenAt ?? null });
      }
    },
    [id, deviceName, work?.title],
  );

  const jumpTo = useCallback(async (c: { raw: string | null; source: string; text: { spine_index: number; char: number } | null }) => {
    const v = view.current;
    if (!v) return;
    if (c.source === "web" && c.raw) await v.goTo(c.raw);
    else if (c.text) {
      const { spine_index, char } = c.text;
      await v.renderer.goTo({ index: spine_index, anchor: (doc) => rangeAt(doc, char) ?? doc.body });
    }
  }, []);

  // Open the book once.
  useEffect(() => {
    if (!Number.isFinite(id)) return;
    let cancelled = false;
    let v: FoliateView | null = null;
    (async () => {
      try {
        // A downloaded book is answered by the service worker from this device.
        const [res, prog] = await Promise.all([
          fetch(`/library/works/${id}/book.epub`, { credentials: "same-origin" }).catch(() => {
            throw new OfflineError();
          }),
          orOffline(() => get<WorkProgressResponse>(`/api/works/${id}/progress`), async () => (await getBook(id))?.progress ?? EMPTY_PROGRESS(id)),
          import("foliate-js/view.js"),
        ]);
        if (!res.ok) {
          let data: { detail?: string; message?: string } = {};
          try {
            data = await res.json();
          } catch {
            /* not json */
          }
          throw new ApiError(res.status, data.message || (res.status === 404 ? "This book has no EPUB to read." : `Could not load the book (${res.status}).`), data.detail ?? null, data, res.headers);
        }
        const blob = await res.blob();
        if (cancelled) return;
        v = document.createElement("foliate-view") as FoliateView;
        v.className = "foliate";
        host.current!.append(v);
        view.current = v;
        v.addEventListener("relocate", (e: Event) => {
          const d = (e as CustomEvent<Relocate>).detail;
          setLoc({ fraction: d.fraction, chapter: d.tocItem?.label?.trim() ?? "" });
          const index = d.section?.current;
          const node = d.range?.startContainer;
          if (index == null || !node) return;
          const doc = node.ownerDocument ?? (node as Document);
          pending.current = { spine_index: index, char: offsetOf(doc, node, d.range.startOffset), raw: d.cfi };
          if (!ready.current) return; // restoring, not reading
          dirty.current = true;
          setHandoff(null);
          if (saveTimer.current) window.clearTimeout(saveTimer.current);
          saveTimer.current = window.setTimeout(() => save().catch(() => {}), 1500);
        });
        v.addEventListener("load", (e: Event) => {
          const doc = (e as CustomEvent<{ doc: Document }>).detail.doc;
          doc.addEventListener("keydown", onKey);
        });
        await v.open(new File([blob], "book.epub", { type: "application/epub+zip" }));
        setToc(v.book.toc ?? []);
        applySettings();

        const mark = readMark(id);
        const hit = target.current;
        if (hit) {
          await v.init({ showTextStart: true });
          await v.renderer.goTo({ index: hit.spine_index, anchor: (doc) => highlight(doc, hit.char, hit.char + hit.length) ?? doc.body });
        } else if (mark?.cfi) await v.init({ lastLocation: mark.cfi });
        else if (prog.source === "web" && prog.raw) await v.init({ lastLocation: prog.raw });
        else if (prog.text) {
          await v.init({ showTextStart: true });
          await jumpTo({ raw: null, source: "server", text: prog.text });
        } else await v.init({ showTextStart: true });
        if (cancelled) return;
        if (!mark && prog.received_at) writeMark(id, { cfi: "", seenAt: prog.received_at });
        const here = view.current ? lastFraction() ?? 0 : null;
        // A search hit is where the person asked to be; no hand-off prompt over it.
        setHandoff(mark && !hit ? pickHandoff(prog, { seenAt: mark.seenAt }, { device: deviceName, mode: "read", here }) : null);
        setLoading(false);
        window.setTimeout(() => (ready.current = true), 600);
      } catch (e) {
        if (!cancelled) {
          setError((e as Error).message);
          setLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
      if (ready.current) save(true).catch(() => {});
      ready.current = false;
      v?.close();
      v?.remove();
      view.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  function lastFraction(): number | null {
    return (view.current as unknown as { lastLocation?: { fraction?: number } })?.lastLocation?.fraction ?? null;
  }

  const applySettings = useCallback(() => {
    const r = view.current?.renderer;
    if (!r) return;
    r.setAttribute("flow", s.flow);
    r.setAttribute("gap", GAP[s.margin]);
    r.setAttribute("max-inline-size", `${WIDTH[s.width]}px`);
    r.setAttribute("max-column-count", s.flow === "paginated" ? "2" : "1");
    r.setAttribute("margin", s.flow === "paginated" ? "44px" : "24px");
    r.setStyles(bookCss(s, theme));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(s), theme]);
  useEffect(applySettings, [applySettings]);

  // Another search hit in this same book while it is open: the reader stays mounted, so go
  // there now (a hit chosen while it is still opening is picked up by the open above).
  useEffect(() => {
    if (atKey === lastAt.current) return;
    lastAt.current = atKey;
    const hit = hitTarget(search);
    target.current = hit;
    const v = view.current;
    if (!hit || !v || loading) return;
    setHandoff(null);
    v.renderer.goTo({ index: hit.spine_index, anchor: (doc) => highlight(doc, hit.char, hit.char + hit.length) ?? doc.body }).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [atKey, loading]);

  // "Use the other device's" in a sync conflict (components/Offline.tsx): go there.
  useEffect(() => {
    const on = (e: Event) => {
      const d = (e as CustomEvent<{ kind: string; id: number; server: { source?: string | null; raw?: string | null; text?: { spine_index: number; char: number } | null } }>).detail;
      if (d.kind !== "work" || d.id !== id || !view.current) return;
      jumpTo({ raw: d.server.raw ?? null, source: d.server.source ?? "", text: d.server.text ?? null }).catch(() => {});
    };
    window.addEventListener("lectern:position", on);
    return () => window.removeEventListener("lectern:position", on);
  }, [id, jumpTo]);

  // Save when the tab hides (phones rarely give an unload).
  useEffect(() => {
    const onHide = () => document.hidden && ready.current && save(true).catch(() => {});
    document.addEventListener("visibilitychange", onHide);
    return () => document.removeEventListener("visibilitychange", onHide);
  }, [save]);

  const onKey = useCallback((e: KeyboardEvent) => {
    const v = view.current;
    if (!v || e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target as HTMLElement | null;
    if (t && t.closest?.("input, select, textarea, [role=dialog]")) return;
    if (e.key === "ArrowLeft" || e.key === "PageUp") {
      e.preventDefault();
      v.goLeft();
    } else if (e.key === "ArrowRight" || e.key === "PageDown" || (e.key === " " && s.flow === "paginated")) {
      e.preventDefault();
      v.goRight();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s.flow]);
  const keyRef = useRef(onKey);
  keyRef.current = onKey;
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (document.querySelector('[aria-modal="true"]')) return;
      if (e.key === "Escape" && !panel) return nav(work ? `/app/library/${work.id}` : "/app/library");
      keyRef.current(e);
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [nav, work, panel]);

  const pct = loc ? Math.round(loc.fraction * 100) : 0;
  useEffect(() => {
    document.title = work ? `${work.title} · Lectern` : "Reader · Lectern";
  }, [work]);

  const p = PALETTE[theme];
  const style = useMemo(() => ({ "--reader-bg": p.bg, "--reader-ink": p.ink } as React.CSSProperties), [p]);

  return (
    <div className="reader" data-reader-theme={theme} data-flow={s.flow} data-chrome={chrome || undefined} style={style}>
      <header className="reader-bar">
        <button className="icon-btn ghost" onClick={() => nav(work ? `/app/library/${work.id}` : "/app/library")} aria-label="Close book" title="Close (Esc)"><ArrowLeft /></button>
        <div className="reader-title">
          <strong>{work?.title ?? "Loading…"}</strong>
          <span>{loc?.chapter || work?.author}</span>
        </div>
        <button className="icon-btn ghost" onClick={() => setPanel("toc")} aria-label="Contents" title="Contents" disabled={!toc.length}><Toc /></button>
        <button className="icon-btn ghost" onClick={() => setPanel("type")} aria-label="Text and layout" title="Text and layout"><TextSize /></button>
      </header>

      {handoff && (
        <div className="handoff reader-handoff" role="status">
          <span>
            <strong>{handoff.device}</strong>, {relativeTime(handoff.received_at, now)}: {handoff.percent != null ? `${Math.round(handoff.percent * 100)}%` : "elsewhere"}
            {handoff.source === "audio" ? " while listening" : ""}
            {handoff.estimated && <span className="hint"> (estimated)</span>}. Continue here?
          </span>
          <span className="row">
            <button className="btn sm primary" onClick={async () => {
              await jumpTo(handoff);
              writeMark(id, { cfi: readMark(id)?.cfi ?? "", seenAt: handoff.received_at });
              setHandoff(null);
            }}>Jump there</button>
            <button className="btn sm ghost" onClick={() => {
              writeMark(id, { cfi: readMark(id)?.cfi ?? "", seenAt: handoff.received_at });
              setHandoff(null);
            }}>Stay here</button>
          </span>
        </div>
      )}

      <div className="reader-stage">
        {s.flow === "paginated" && !loading && !error && (
          <button className="page-zone left" aria-label="Previous page" onClick={() => view.current?.goLeft()}><Chevron /></button>
        )}
        <div className="reader-host" ref={host} onDoubleClick={() => setChrome((c) => !c)} />
        {s.flow === "paginated" && !loading && !error && (
          <button className="page-zone right" aria-label="Next page" onClick={() => view.current?.goRight()}><Chevron /></button>
        )}
        {loading && !error && <div className="reader-msg" aria-busy="true"><span className="spinner" aria-hidden="true" /> Opening the book…</div>}
        {error && (
          <div className="reader-msg" role="alert">
            <p>{error}</p>
            <button className="btn" onClick={() => nav("/app/library")}>Back to the library</button>
          </div>
        )}
      </div>

      <footer className="reader-foot">
        <input
          type="range"
          min={0}
          max={1000}
          value={Math.round((loc?.fraction ?? 0) * 1000)}
          aria-label="Position in book"
          aria-valuetext={`${pct}%`}
          style={{ "--fill": `${pct}%` } as React.CSSProperties}
          onChange={(e) => view.current?.goToFraction(Number(e.target.value) / 1000)}
          disabled={loading || !!error}
        />
        <span className="tabular">{pct}%</span>
      </footer>

      <Sheet open={panel === "toc"} onClose={() => setPanel(null)} title="Contents" labelledBy="toc-title">
        <TocList items={toc} current={loc?.chapter} onPick={(href) => { view.current?.goTo(href); setPanel(null); }} />
      </Sheet>
      <Sheet open={panel === "type"} onClose={() => setPanel(null)} title="Text and layout" labelledBy="type-title">
        <TypePanel s={s} set={set} reset={() => { setSettings(DEFAULTS); toast("Reader settings reset"); }} />
      </Sheet>
    </div>
  );
}

function TocList({ items, current, onPick, depth = 0 }: { items: TocItem[]; current?: string; onPick: (href: string) => void; depth?: number }) {
  return (
    <ul className="toc" data-depth={depth}>
      {items.map((it, i) => (
        <li key={`${it.href}-${i}`}>
          <button aria-current={current && it.label?.trim() === current ? "location" : undefined} onClick={() => onPick(it.href)}>{it.label?.trim() || "Untitled"}</button>
          {it.subitems?.length ? <TocList items={it.subitems} current={current} onPick={onPick} depth={depth + 1} /> : null}
        </li>
      ))}
    </ul>
  );
}

function Seg<T extends string>({ label, value, options, onChange }: { label: string; value: T; options: [T, string][]; onChange: (v: T) => void }) {
  return (
    <div className="type-row">
      <span id={`seg-${label}`}>{label}</span>
      <div className="seg" role="radiogroup" aria-labelledby={`seg-${label}`}>
        {options.map(([v, l]) => (
          <button key={v} role="radio" aria-checked={value === v} aria-pressed={value === v} onClick={() => onChange(v)}>{l}</button>
        ))}
      </div>
    </div>
  );
}

function TypePanel({ s, set, reset }: { s: ReaderSettings; set: <K extends keyof ReaderSettings>(k: K, v: ReaderSettings[K]) => void; reset: () => void }) {
  return (
    <div className="type-panel">
      <div className="type-row">
        <span id="theme-l">Theme</span>
        <div className="swatches" role="radiogroup" aria-labelledby="theme-l">
          {(["auto", "light", "sepia", "dark"] as ReaderTheme[]).map((t) => (
            <button key={t} role="radio" aria-checked={s.theme === t} className={`swatch ${t}`} onClick={() => set("theme", t)}>
              <span aria-hidden="true">Aa</span>
              {t === "auto" ? "Match app" : t === "dark" ? "Warm dark" : t === "sepia" ? "Warm" : "Light"}
            </button>
          ))}
        </div>
      </div>
      <Seg label="Layout" value={s.flow} options={[["paginated", "Pages"], ["scrolled", "Scroll"]]} onChange={(v) => set("flow", v)} />
      <Seg label="Font" value={s.font} options={[["publisher", "Book’s own"], ["serif", "Serif"], ["sans", "Sans"]]} onChange={(v) => set("font", v)} />
      <div className="type-row">
        <label htmlFor="size">Size <span className="hint tabular">{s.size}%</span></label>
        <div className="row">
          <button className="btn sm" aria-label="Smaller text" onClick={() => set("size", Math.max(70, s.size - 10))}>A−</button>
          <input id="size" type="range" min={70} max={200} step={5} value={s.size} onChange={(e) => set("size", Number(e.target.value))} />
          <button className="btn sm" aria-label="Larger text" onClick={() => set("size", Math.min(200, s.size + 10))}>A+</button>
        </div>
      </div>
      <div className="type-row">
        <label htmlFor="lh">Line spacing <span className="hint tabular">{s.lineHeight.toFixed(1)}</span></label>
        <input id="lh" type="range" min={1.1} max={2.2} step={0.1} value={s.lineHeight} onChange={(e) => set("lineHeight", Number(e.target.value))} />
      </div>
      <Seg label="Margins" value={s.margin} options={[["small", "Small"], ["normal", "Normal"], ["large", "Large"]]} onChange={(v) => set("margin", v)} />
      <Seg label="Line length" value={s.width} options={[["narrow", "Narrow"], ["normal", "Normal"], ["wide", "Wide"]]} onChange={(v) => set("width", v)} />
      <div className="type-row">
        <span id="just-l">Justify text</span>
        <button className="switch" role="switch" aria-checked={s.justify} aria-labelledby="just-l" onClick={() => set("justify", !s.justify)}><span /></button>
      </div>
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button className="btn sm ghost" onClick={reset}>Reset</button>
      </div>
      <p className="hint">Settings stay on this browser. Use ← and → (or the page edges) to turn pages.</p>
    </div>
  );
}
