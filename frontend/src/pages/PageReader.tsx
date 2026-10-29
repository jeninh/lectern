// Page viewer for PDFs (pdf.js, rendered to canvases) and CBZ comics (the page images the server
// serves after checking the archive). Page navigation, fit width / fit page, zoom, and a
// two-page spread (right to left for manga). Progress is a page number, saved to the same
// KOReader record the e-reader syncs (PUT /api/files/:id/progress), with a hand-off prompt
// instead of a silent jump when another device is further along.
//
// pdf.js runs here without its viewer layer: only the canvas renderer, with no annotation or
// form layer and no scripting sandbox (pdf.sandbox is never loaded), so nothing in a PDF runs.
// pdf.js 6 has no eval-based code path, and the CSP keeps scripts same-origin.
//
// Read-along on PDF pages (docs/pdf-read-along.md): when the work has aligned (or generated)
// audio and Lectern read this PDF's text layer, a player plays the work's audio and the current
// sentence is highlighted over the canvas from the boxes the server sends; the pages follow the
// narration (toggle with F), and clicking a sentence plays from it. Downloaded books keep the
// boxes on the device, so this works offline too.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { DictionaryPopover, type LookupRequest } from "../components/DictionaryPopover";
import { ArrowLeft, Chevron, Follow, NextSentence, Pause, Play, PrevSentence } from "../components/Icons";
import { ApiError, get } from "../lib/api";
import { wordAt } from "../lib/dictionary";
import { sentenceAround } from "../lib/vocabulary";
import { getPdfInfo, orOffline } from "../lib/offline/store";
import { savePosition } from "../lib/offline/sync";
import { relativeTime } from "../lib/format";
import { useLocalStorage } from "../lib/hooks";
import { PALETTE, resolveTheme, useReaderSettings } from "../lib/reader";
import {
  type Affine,
  boxesFor,
  buildTimeline,
  hit,
  loadPdfReadAlong,
  pageOf,
  type PdfReadAlongData,
  startOf,
  toPdfPoint,
  toRect,
  workSentenceAt,
} from "../lib/pdfreadalong";
import { formatClock, locate, saveProgress, spokenDuration } from "../lib/readalong";
import { clampPage, findInItems, type Fit, fitScale, multiply, pageLabel, spreadOf, stepZoom, turn } from "../lib/pages";
import { useStore } from "../lib/store";
import type { PagesInfo, PdfBox } from "../lib/types";

interface ViewSettings {
  fit: Fit;
  zoom: number;
  spread: boolean;
}
const DEFAULTS: Record<"PDF" | "CBZ", ViewSettings> = {
  PDF: { fit: "width", zoom: 1, spread: false },
  CBZ: { fit: "page", zoom: 1, spread: false },
};
interface Mark {
  page: number;
  seenAt: string | null;
}
const markKey = (fileId: number) => `lectern.page.${fileId}`;
function readMark(fileId: number): Mark | null {
  try {
    return JSON.parse(localStorage.getItem(markKey(fileId)) || "null");
  } catch {
    return null;
  }
}
function writeMark(fileId: number, m: Mark) {
  try {
    localStorage.setItem(markKey(fileId), JSON.stringify(m));
  } catch {
    /* quota */
  }
}

/* Minimal pdf.js surface (the package ships types, but loading it lazily keeps them out). */
interface PdfTextItem {
  str: string;
  transform: number[];
  width: number;
}
interface PdfPage {
  getViewport(o: { scale: number }): { width: number; height: number; scale: number; transform: number[] };
  getTextContent(): Promise<{ items: (PdfTextItem | { type: string })[] }>;
  render(o: {
    canvas: HTMLCanvasElement;
    canvasContext: CanvasRenderingContext2D;
    viewport: unknown;
    transform?: number[];
    pageColors?: { background: string; foreground: string };
  }): { promise: Promise<void>; cancel(): void };
}
interface PdfDoc {
  numPages: number;
  getPage(n: number): Promise<PdfPage>;
}
/** pdf.js 6: the loading task owns the worker and transport; destroying it frees the document. */
interface PdfTask {
  promise: Promise<PdfDoc>;
  destroy(): Promise<void>;
}

async function openPdf(url: string): Promise<PdfTask> {
  const [pdfjs, worker] = await Promise.all([import("pdfjs-dist"), import("pdfjs-dist/build/pdf.worker.min.mjs?url")]);
  pdfjs.GlobalWorkerOptions.workerSrc = worker.default;
  const assets = `${import.meta.env.BASE_URL}pdfjs/`;
  const task = pdfjs.getDocument({
    url,
    withCredentials: true,
    cMapUrl: `${assets}cmaps/`,
    cMapPacked: true,
    standardFontDataUrl: `${assets}standard_fonts/`,
    wasmUrl: `${assets}wasm/`,
    enableXfa: false,
  });
  return task as unknown as PdfTask;
}

const SAVE_EVERY_MS = 15000;
const SOURCES = { web: "Lectern", audio: "the audiobook", koreader: "KOReader" } as const;

export function PageReader({ workId, fileId }: { workId: number; fileId: number }) {
  const nav = useNavigate();
  const { deviceName, now, resolvedTheme, works } = useStore();
  // The reader's theme for this book (lib/reader.ts) colours the chrome around the pages.
  const readerTheme = resolveTheme(useReaderSettings(workId).settings.theme, resolvedTheme);
  // Word lookup: double-click a word on a PDF page (pdf.js's reading of its text).
  const [lookup, setLookup] = useState<LookupRequest | null>(null);
  const closeLookup = useCallback(() => setLookup(null), []);
  const toggleChrome = useCallback(() => setChrome((c) => !c), []);
  // Opened from a search hit ("In books" in Cmd/Ctrl+K): that page, with the match marked.
  const [search] = useSearchParams();
  const findPage = Number(search.get("page")) || 0;
  const findText = search.get("find") ?? "";
  const jumpTo = useRef({ page: findPage, find: findText });
  const lastJump = useRef(`${findPage}|${findText}`);
  const [info, setInfo] = useState<PagesInfo | null>(null);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [chrome, setChrome] = useState(true);
  const [handoff, setHandoff] = useState<PagesInfo["progress"]>(null);
  const fmt = info?.format ?? "PDF";
  const [stored, setStored] = useLocalStorage<Record<string, ViewSettings>>("lectern.pages", {});
  const view: ViewSettings = { ...DEFAULTS[fmt], ...stored[fmt] };
  const setView = (patch: Partial<ViewSettings>) => setStored({ ...stored, [fmt]: { ...view, ...patch } });
  const pdf = useRef<PdfDoc | null>(null);
  const pdfTask = useRef<PdfTask | null>(null);
  const stage = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const dirty = useRef(false);
  const saveTimer = useRef<number | null>(null);
  const latest = useRef({ page: 1, total: 0 });
  latest.current = { page, total };

  // Open: the page list and synced position, then the PDF itself.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const i = await orOffline(() => get<PagesInfo>(`/api/files/${fileId}/pages`), () => getPdfInfo(fileId));
        if (cancelled) return;
        if (i.work_id !== workId) throw new ApiError(404, "That file belongs to another book.", null, {}, new Headers());
        setInfo(i);
        let n = i.pages ?? 0;
        if (i.url) {
          const task = await openPdf(i.url);
          pdfTask.current = task;
          const doc = await task.promise;
          if (cancelled) return;
          pdf.current = doc;
          n = doc.numPages;
        }
        setTotal(n);
        const mark = readMark(fileId);
        const server = i.progress;
        const start = clampPage(jumpTo.current.page || (mark?.page ?? server?.page ?? 1), n);
        setPage(start);
        if (!jumpTo.current.page && mark && server?.page && server.page !== start && server.updated_at && (!mark.seenAt || server.updated_at > mark.seenAt)) setHandoff(server);
        if (!mark && server?.updated_at) writeMark(fileId, { page: start, seenAt: server.updated_at });
        // Newer than anything on the page record: listening, the EPUB, or KOReader moved on.
        const pos = i.position;
        const seen = mark?.seenAt ?? server?.updated_at ?? null;
        if (!jumpTo.current.page && pos && pos.page !== start && pos.updated_at && (!seen || pos.updated_at > seen)) {
          setHandoff({ page: pos.page, percentage: 0, device: pos.device || SOURCES[pos.source], from_web: false, updated_at: pos.updated_at });
        }
        setLoading(false);
      } catch (e) {
        if (!cancelled) {
          setError((e as Error).message || "Could not open this book.");
          setLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
      pdfTask.current?.destroy().catch(() => {});
      pdfTask.current = null;
      pdf.current = null;
    };
  }, [fileId, workId]);

  // Another search hit in this same PDF while it is open: the viewer stays mounted, so turn
  // to that page now (a hit chosen while it is still opening is picked up by the open above).
  useEffect(() => {
    const key = `${findPage}|${findText}`;
    if (key === lastJump.current) return;
    lastJump.current = key;
    jumpTo.current = { page: findPage, find: findText };
    if (!findPage || loading || !latest.current.total) return;
    setHandoff(null);
    setPage(clampPage(findPage, latest.current.total));
  }, [findPage, findText, loading]);

  // Stage size, for fitting.
  useEffect(() => {
    const el = stage.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setSize({ w: e.contentRect.width, h: e.contentRect.height }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const save = useCallback(async (keepalive = false) => {
    const { page: p, total: n } = latest.current;
    if (!dirty.current || !n) return;
    dirty.current = false;
    try {
      const r = await savePosition<NonNullable<PagesInfo["progress"]>>({ kind: "page", id: fileId, workId, title: info?.title ?? "this book" }, { page: p, pages: n, device: deviceName }, keepalive);
      writeMark(fileId, { page: p, seenAt: r?.updated_at ?? readMark(fileId)?.seenAt ?? null });
    } catch {
      writeMark(fileId, { page: p, seenAt: readMark(fileId)?.seenAt ?? null });
    }
  }, [fileId, deviceName, workId, info?.title]);

  // "Use the other device's" in a sync conflict (components/Offline.tsx): go to its page.
  useEffect(() => {
    const on = (e: Event) => {
      const d = (e as CustomEvent<{ kind: string; id: number; server: { page?: number | null } }>).detail;
      if (d.kind === "page" && d.id === fileId && d.server.page) setPage(clampPage(d.server.page, latest.current.total));
    };
    window.addEventListener("lectern:position", on);
    return () => window.removeEventListener("lectern:position", on);
  }, [fileId]);

  const go = useCallback((p: number) => {
    const next = clampPage(p, latest.current.total);
    setPage(next);
    writeMark(fileId, { page: next, seenAt: readMark(fileId)?.seenAt ?? null });
    dirty.current = true;
    setHandoff(null);
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => save().catch(() => {}), 1200);
  }, [fileId, save]);

  useEffect(() => {
    const onHide = () => document.hidden && save(true).catch(() => {});
    document.addEventListener("visibilitychange", onHide);
    return () => {
      document.removeEventListener("visibilitychange", onHide);
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
      save(true).catch(() => {});
    };
  }, [save]);

  const rtl = !!info?.rtl;
  const shown = useMemo(() => (total ? spreadOf(page, total, view.spread) : []), [page, total, view.spread]);
  const display = rtl ? [...shown].reverse() : shown;
  const forward = useCallback(() => go(turn(latest.current.page, latest.current.total, view.spread, 1)), [go, view.spread]);
  const back = useCallback(() => go(turn(latest.current.page, latest.current.total, view.spread, -1)), [go, view.spread]);
  // Screen left/right: in a right-to-left comic the next page is on the left.
  const left = rtl ? forward : back;
  const right = rtl ? back : forward;

  // --- read-along on the pages -------------------------------------------------------------
  const [ra, setRa] = useState<PdfReadAlongData | null>(null);
  const tl = useMemo(() => (ra ? buildTimeline(ra.sentences, ra.pdf.sentences) : null), [ra]);
  const [follow, setFollow] = useLocalStorage<boolean>("lectern.pdf.follow", true);
  const audio = useRef<HTMLAudioElement>(null);
  const trackIndex = useRef(0);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [current, setCurrent] = useState(-1);
  const currentRef = useRef(-1);
  const lastSave = useRef({ at: 0, n: -1 });
  const followRef = useRef(follow);
  followRef.current = follow;
  const readalong = !!(info?.text?.readalong && tl && tl.matched.length);
  const tracks = ra?.tracks.tracks ?? [];
  const duration = ra?.tracks.total_duration_s ?? 0;

  // Only once the document is open: opening sets the starting page, which would undo a turn.
  useEffect(() => {
    if (!info?.text?.readalong || loading || error) return;
    let cancelled = false;
    loadPdfReadAlong(workId, fileId)
      .then((d) => !cancelled && d.tracks.tracks.length && setRa(d))
      .catch(() => {}); // no read-along: the pages still read
    return () => {
      cancelled = true;
    };
  }, [info?.text?.readalong, loading, error, workId, fileId]);

  const clock = () => (audio.current && tracks.length ? tracks[trackIndex.current].offset_s + audio.current.currentTime : 0);

  const saveAudio = useCallback((keepalive = false) => {
    const n = currentRef.current;
    if (n < 0) return;
    lastSave.current = { at: Date.now(), n };
    saveProgress(workId, "audio", { sentence_n: n }, keepalive, info?.title).catch(() => {});
  }, [workId, info?.title]);

  /** Light up work sentence `n` and, when following, turn to its page. */
  const mark = useCallback((n: number, turnPage: boolean) => {
    if (n === currentRef.current || !tl) return;
    currentRef.current = n;
    setCurrent(n);
    const p = turnPage ? pageOf(tl, n) : null;
    const { page: at, total: count } = latest.current;
    if (p && !spreadOf(at, count, view.spread).includes(p)) go(p);
  }, [tl, go, view.spread]);

  const seek = useCallback((t: number, play?: boolean) => {
    const a = audio.current;
    if (!a || !tracks.length || !tl) return;
    const { index, local } = locate(tracks, Math.max(0, Math.min(t, duration - 0.05)));
    const start = () => {
      a.currentTime = local;
      if (play) a.play().catch(() => {});
    };
    if (index !== trackIndex.current || !a.src) {
      trackIndex.current = index;
      a.src = tracks[index].stream_url;
      a.addEventListener("loadedmetadata", start, { once: true });
      a.load();
    } else start();
    setTime(t);
    mark(workSentenceAt(tl, t), followRef.current);
  }, [tracks, duration, tl, mark]);

  const playSentence = useCallback((n: number) => {
    const t = tl ? startOf(tl, n) : null;
    if (t != null) seek(t + 0.001, true);
  }, [tl, seek]);

  const step = useCallback((delta: 1 | -1) => {
    if (!tl) return;
    const i = Math.max(0, tl.matched.findIndex((s) => s.n >= currentRef.current));
    const next = tl.matched[Math.max(0, Math.min(tl.matched.length - 1, (currentRef.current < 0 ? -1 : i) + delta))];
    if (next) seek((next.audio_start_s as number) + 0.001, !audio.current?.paused);
  }, [tl, seek]);

  const toggle = useCallback(() => {
    const a = audio.current;
    if (!a) return;
    if (a.paused) {
      if (!a.src) seek(time, true);
      else a.play().catch(() => {});
      // Playing again brings the narration back into view.
      if (followRef.current && tl && currentRef.current >= 0) {
        const p = pageOf(tl, currentRef.current);
        if (p && !spreadOf(latest.current.page, latest.current.total, view.spread).includes(p)) go(p);
      }
    } else a.pause();
  }, [seek, time, tl, go, view.spread]);

  // Where the audio was left (no autoplay, and no page jump: the page record decides that).
  useEffect(() => {
    if (!ra || !tl || !audio.current) return;
    const p = ra.progress;
    const n = p?.position?.sentence_n ?? -1;
    const t = p?.audio_s ?? (n >= 0 ? startOf(tl, n) : null) ?? tl.starts[0] ?? 0;
    const { index, local } = locate(ra.tracks.tracks, t);
    const a = audio.current;
    trackIndex.current = index;
    a.src = ra.tracks.tracks[index].stream_url;
    a.addEventListener("loadedmetadata", () => (a.currentTime = local), { once: true });
    setTime(t);
    mark(workSentenceAt(tl, t), false);
  }, [ra]);

  // Follow the audio clock every frame while playing.
  useEffect(() => {
    if (!playing || !tl) return;
    let raf = 0;
    let lastUi = 0;
    const tick = (ts: number) => {
      const t = clock();
      mark(workSentenceAt(tl, t), followRef.current);
      if (ts - lastUi > 250) {
        lastUi = ts;
        setTime(t);
      }
      if (Date.now() - lastSave.current.at > SAVE_EVERY_MS && lastSave.current.n !== currentRef.current) saveAudio();
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing, tl, mark, saveAudio]);

  const onEnded = () => {
    const a = audio.current;
    if (!a) return;
    if (trackIndex.current < tracks.length - 1) {
      trackIndex.current += 1;
      a.src = tracks[trackIndex.current].stream_url;
      a.play().catch(() => {});
    } else {
      setPlaying(false);
      saveAudio();
    }
  };

  const toggleFollow = useCallback(() => {
    const next = !followRef.current;
    setFollow(next);
    if (next && tl && currentRef.current >= 0) {
      const p = pageOf(tl, currentRef.current);
      if (p && !spreadOf(latest.current.page, latest.current.total, view.spread).includes(p)) go(p);
    }
  }, [setFollow, tl, go, view.spread]);

  // Following: keep the highlight in view when a zoomed or fit-width page is taller than the stage.
  // Called by a page once it has drawn the highlight (pages render asynchronously).
  const keepInView = useCallback((marks: HTMLElement) => {
    const el = stage.current;
    const rects = [...marks.children].map((c) => c.getBoundingClientRect());
    if (!el || !followRef.current || !rects.length) return;
    const box = el.getBoundingClientRect();
    const r = { top: Math.min(...rects.map((x) => x.top)), bottom: Math.max(...rects.map((x) => x.bottom)) };
    if (r.top >= box.top + 16 && r.bottom <= box.bottom - 16) return;
    el.scrollTo({ top: el.scrollTop + r.top - box.top - box.height * 0.3, behavior: "smooth" });
  }, []);

  /** The highlight on page `n` (1-based). */
  const marksOn = (n: number): PdfBox[] => (readalong && tl && current >= 0 ? boxesFor(tl, current).filter((b) => b[0] === n - 1) : []);
  const pick = useCallback((n: number, x: number, y: number) => {
    if (!tl) return false;
    const w = hit(tl, n, x, y);
    if (w == null) return false;
    playSentence(w);
    return true;
  }, [tl, playSentence]);

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (document.querySelector('[aria-modal="true"]') || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t?.closest?.("input, select, textarea")) return;
      if (e.key === "Escape") return nav(`/app/library/${workId}`);
      const map: Record<string, () => void> = {
        ArrowLeft: left, ArrowRight: right, PageUp: back, PageDown: forward, " ": readalong ? toggle : forward,
        ...(readalong ? { f: toggleFollow, F: toggleFollow, ",": () => step(-1), ".": () => step(1) } : {}),
        Home: () => go(1), End: () => go(latest.current.total),
        "+": () => setView({ zoom: stepZoom(view.zoom, 1) }), "=": () => setView({ zoom: stepZoom(view.zoom, 1) }),
        "-": () => setView({ zoom: stepZoom(view.zoom, -1) }), "0": () => setView({ zoom: 1 }),
      };
      const fn = map[e.key];
      if (fn) {
        e.preventDefault();
        fn();
      }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  });

  useEffect(() => {
    document.title = info ? `${info.title} · Lectern` : "Reader · Lectern";
  }, [info]);
  useEffect(() => {
    stage.current?.scrollTo({ top: 0 });
  }, [page]);

  const pct = total > 1 ? Math.round(((page - 1) / (total - 1)) * 100) : 0; // where the slider thumb sits
  const theme = readerTheme;
  // Light themes get a slightly darker stage than their paper, so white pages stand out on it.
  const stageBg = { light: "#f3efe7", contrast: "#e4e4e4" } as Partial<Record<typeof theme, string>>;
  const style = { "--reader-bg": stageBg[theme] ?? PALETTE[theme].bg, "--reader-ink": PALETTE[theme].ink } as React.CSSProperties;

  return (
    <div className="reader pages-reader" data-reader-theme={theme} data-chrome={chrome || undefined} data-fit={view.fit} style={style}>
      <header className="reader-bar pages-bar">
        <button className="icon-btn ghost" onClick={() => nav(`/app/library/${workId}`)} aria-label="Close book" title="Close (Esc)"><ArrowLeft /></button>
        <div className="reader-title">
          <strong>{info?.title ?? "Loading…"}</strong>
          <span>{info ? `${fmt === "CBZ" ? "Comic" : "PDF"}${total ? ` · ${pageLabel(shown, total)}` : ""}` : ""}</span>
        </div>
        <div className="pages-tools" role="toolbar" aria-label="View">
          <div className="seg" role="radiogroup" aria-label="Fit">
            <button role="radio" aria-checked={view.fit === "width"} aria-pressed={view.fit === "width"} onClick={() => setView({ fit: "width" })} title="Fit width">Width</button>
            <button role="radio" aria-checked={view.fit === "page"} aria-pressed={view.fit === "page"} onClick={() => setView({ fit: "page" })} title="Fit page">Page</button>
          </div>
          <button className="btn sm ghost" aria-pressed={view.spread} onClick={() => setView({ spread: !view.spread })} title="Show two pages side by side">Two pages</button>
          <span className="pages-zoom">
            <button className="icon-btn ghost sm" onClick={() => setView({ zoom: stepZoom(view.zoom, -1) })} aria-label="Zoom out" title="Zoom out (−)">−</button>
            <button className="btn sm ghost tabular" onClick={() => setView({ zoom: 1 })} aria-label={`Zoom ${Math.round(view.zoom * 100)}%, reset`} title="Reset zoom (0)">{Math.round(view.zoom * 100)}%</button>
            <button className="icon-btn ghost sm" onClick={() => setView({ zoom: stepZoom(view.zoom, 1) })} aria-label="Zoom in" title="Zoom in (+)">+</button>
          </span>
        </div>
      </header>

      {handoff && (
        <div className="handoff reader-handoff" role="status">
          <span><strong>{handoff.device}</strong>, {relativeTime(handoff.updated_at, now)}: page {handoff.page}. Continue there?</span>
          <span className="row">
            <button className="btn sm primary" onClick={() => { writeMark(fileId, { page: handoff.page!, seenAt: handoff.updated_at }); setPage(clampPage(handoff.page!, total)); setHandoff(null); }}>Jump there</button>
            <button className="btn sm ghost" onClick={() => { writeMark(fileId, { page, seenAt: handoff.updated_at }); setHandoff(null); }}>Stay here</button>
          </span>
        </div>
      )}

      {lookup && <DictionaryPopover request={lookup} lang={works.find((w) => w.id === workId)?.language} onClose={closeLookup} />}

      <div className="reader-stage">
        {!loading && !error && total > 0 && (
          <>
            <button className="page-zone left" aria-label={rtl ? "Next page" : "Previous page"} onClick={left}><Chevron /></button>
            <button className="page-zone right" aria-label={rtl ? "Previous page" : "Next page"} onClick={right}><Chevron /></button>
          </>
        )}
        <div className="pages-stage" ref={stage} onDoubleClick={toggleChrome}>
          {!loading && !error && total > 0 && (
            <div className="pages-spread" data-count={display.length} dir="ltr">
              {display.map((n) =>
                info?.url ? (
                  <PdfCanvas key={n} doc={pdf.current!} n={n} count={display.length} fit={view.fit} zoom={view.zoom} stage={size} dark={theme === "dark"} marks={marksOn(n)} find={n === findPage ? findText : ""} onPick={readalong ? pick : undefined} onMarked={keepInView} onWord={setLookup} onMiss={toggleChrome} workId={workId} fileId={fileId} />
                ) : (
                  <ComicPage key={n} fileId={fileId} n={n} count={display.length} fit={view.fit} zoom={view.zoom} stage={size} />
                ),
              )}
            </div>
          )}
        </div>
        {loading && !error && <div className="reader-msg" aria-busy="true"><span className="spinner" aria-hidden="true" /> Opening the book…</div>}
        {error && (
          <div className="reader-msg" role="alert">
            <p>{error}</p>
            <button className="btn" onClick={() => nav(`/app/library/${workId}`)}>Back to the book</button>
          </div>
        )}
      </div>

      {readalong && (
        <div className="pdf-ra-bar" role="region" aria-label="Read-along player" data-sentence={current}>
          <button className="icon-btn ghost" onClick={() => step(-1)} aria-label="Previous sentence" title="Previous sentence (,)"><PrevSentence /></button>
          <button className="ra-play sm" onClick={toggle} aria-label={playing ? "Pause" : "Play"} title={`${playing ? "Pause" : "Play"} (Space)`}>{playing ? <Pause /> : <Play />}</button>
          <button className="icon-btn ghost" onClick={() => step(1)} aria-label="Next sentence" title="Next sentence (.)"><NextSentence /></button>
          <span className="ra-time tabular" aria-hidden="true">{formatClock(time)}</span>
          <input
            type="range"
            min={0}
            max={duration}
            step={0.1}
            value={Math.min(time, duration)}
            onChange={(e) => seek(Number(e.target.value), playing)}
            aria-label="Narration position"
            aria-valuetext={`${spokenDuration(time)} of ${spokenDuration(duration)}`}
          />
          <span className="ra-time tabular" aria-hidden="true">{formatClock(duration)}</span>
          <button className="icon-btn ghost" aria-pressed={follow} onClick={toggleFollow} aria-label="Follow the narration" title="Turn pages with the narration (F)"><Follow /></button>
          <span className="sr-only">Click a sentence on the page to play from it.</span>
        </div>
      )}
      {ra && (
        <audio
          ref={audio}
          preload="metadata"
          onPlay={() => setPlaying(true)}
          onPause={() => (setPlaying(false), setTime(clock()), saveAudio())}
          onEnded={onEnded}
          onSeeked={() => setTime(clock())}
        />
      )}

      <footer className="reader-foot pages-foot">
        <input
          type="range"
          min={1}
          max={Math.max(1, total)}
          value={page}
          dir={rtl ? "rtl" : "ltr"}
          aria-label="Page"
          aria-valuetext={total ? pageLabel(shown, total) : ""}
          style={{ "--fill": `${rtl ? 100 - pct : pct}%` } as React.CSSProperties}
          onChange={(e) => go(Number(e.target.value))}
          disabled={loading || !!error || total < 2}
        />
        <label className="pages-goto">
          <span className="sr-only">Go to page</span>
          <input type="number" min={1} max={total || 1} value={page} onChange={(e) => e.target.value && go(Number(e.target.value))} disabled={loading || !!error} />
          <span className="tabular">/ {total || "–"}</span>
        </label>
      </footer>
    </div>
  );
}

interface PageProps {
  n: number;
  count: number;
  fit: Fit;
  zoom: number;
  stage: { w: number; h: number };
}

interface PdfCanvasProps extends PageProps {
  doc: PdfDoc;
  dark: boolean;
  /** Read-along: the current sentence's boxes on this page. */
  marks: PdfBox[];
  /** Read-along: a click at PDF point (x, y) on page n; true when it hit a sentence. */
  onPick?: (n: number, x: number, y: number) => boolean;
  /** Read-along: the highlight is drawn (the element holding its rectangles). */
  onMarked?: (marks: HTMLElement) => void;
  /** A search match to mark on this page (opened from "In books" in Cmd/Ctrl+K). */
  find: string;
  /** Word lookup: the word double-clicked on this page, where it is on screen. */
  onWord?: (r: LookupRequest) => void;
  /** The book and file this page belongs to, for saving a word with its place. */
  workId?: number;
  fileId?: number;
  /** A double-click with no word under it (blank space, an image, a scan): what the stage's
   * own double-click does (show or hide the toolbars). */
  onMiss?: () => void;
}

export function PdfCanvas({ doc, n, count, fit, zoom, stage, dark, marks, onPick, onMarked, find, onWord, onMiss, workId, fileId }: PdfCanvasProps) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [found, setFound] = useState<{ x: number; y: number; w: number; h: number }[]>([]);
  const markLayer = useRef<HTMLDivElement>(null);
  const [css, setCss] = useState<{ w: number; h: number; t: Affine } | null>(null);
  const marked = marks.map((m) => m.join()).join(";");
  useEffect(() => {
    if (css && markLayer.current) onMarked?.(markLayer.current);
  }, [css, marked]);
  useEffect(() => {
    let cancelled = false;
    let task: { cancel(): void } | null = null;
    // Marks belong to one query at one size: none from before this change while the page
    // redraws, and only this run's (a superseded run is cancelled before it can set any).
    setFound([]);
    (async () => {
      const page = await doc.getPage(n);
      if (cancelled || !canvas.current) return;
      const base = page.getViewport({ scale: 1 });
      const scale = fitScale(fit, base.width * count, base.height, stage.w - 8, stage.h - 8, zoom);
      const vp = page.getViewport({ scale });
      const dpr = Math.min(window.devicePixelRatio || 1, 3);
      const c = canvas.current;
      c.width = Math.floor(vp.width * dpr);
      c.height = Math.floor(vp.height * dpr);
      setCss({ w: vp.width, h: vp.height, t: vp.transform as Affine });
      const r = page.render({
        canvas: c,
        canvasContext: c.getContext("2d")!,
        viewport: vp,
        transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined,
        // Dark mode: light text on a dark page (images keep their colours).
        pageColors: dark ? { background: "#1f1b16", foreground: "#e6dccb" } : undefined,
      });
      task = r;
      await r.promise.catch(() => {});
      if (!find || cancelled) return;
      // The match's boxes, from pdf.js's own reading of the page's text (no text layer is
      // rendered): each item's baseline origin and width, in viewport pixels.
      const items = (await page.getTextContent()).items.filter((it): it is PdfTextItem => "str" in it);
      if (cancelled) return;
      // Where a piece starts and ends inside its item: the item's width split in proportion to
      // the text's width in a sans-serif (close for proportional fonts, exact for monospace).
      const measure = document.createElement("canvas").getContext("2d")!;
      measure.font = "100px sans-serif";
      const part = (str: string, upto: number) => measure.measureText(str.slice(0, upto)).width / Math.max(1, measure.measureText(str).width);
      setFound(
        findInItems(items, find).map(({ item, from, to }) => {
          const it = items[item];
          const t = multiply(vp.transform, it.transform);
          const h = Math.hypot(t[2], t[3]);
          const w = it.width * vp.scale;
          const a = part(it.str, from);
          return { x: t[4] + w * a, y: t[5] - h, w: w * (part(it.str, to) - a), h: h * 1.2 };
        }),
      );
    })().catch(() => {});
    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [doc, n, count, fit, zoom, stage.w, stage.h, dark, find]);
  const click = (e: React.MouseEvent<HTMLDivElement>) => {
    // The second click of a double-click is a word lookup, not another read-along pick.
    if (!onPick || !css || e.detail > 1 || window.getSelection()?.toString()) return;
    const r = e.currentTarget.getBoundingClientRect();
    const [x, y] = toPdfPoint(css.t, e.clientX - r.left, e.clientY - r.top);
    if (onPick(n, x, y)) e.stopPropagation();
  };
  const dblclick = async (e: React.MouseEvent<HTMLDivElement>) => {
    if (!onWord || !css) return;
    const r = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - r.left;
    const y = e.clientY - r.top;
    const { clientX, clientY } = e;
    // Whether a word is here is known only once the text is read: the stage's show/hide
    // toolbars waits, and onMiss does it when there is none.
    e.stopPropagation();
    let items: PdfTextItem[] = [];
    try {
      const page = await doc.getPage(n);
      items = (await page.getTextContent()).items.filter((it): it is PdfTextItem => "str" in it && !!it.str);
    } catch {
      /* no text: a miss */
    }
    const scale = Math.hypot(css.t[0], css.t[1]);
    const measure = document.createElement("canvas").getContext("2d")!;
    measure.font = "100px sans-serif";
    for (const it of items) {
      const t = multiply(css.t, it.transform);
      const h = Math.hypot(t[2], t[3]);
      const w = it.width * scale;
      if (x < t[4] || x > t[4] + w || y < t[5] - h || y > t[5] + h * 0.3) continue;
      // The character under the point: the item's width split as in the search marks above.
      const full = Math.max(1, measure.measureText(it.str).width);
      const frac = (x - t[4]) / Math.max(1, w);
      let k = 0;
      while (k < it.str.length - 1 && measure.measureText(it.str.slice(0, k + 1)).width / full < frac) k++;
      const query = wordAt(it.str, k);
      if (!query) break;
      // The sentence around the word, in the page's text items joined by spaces.
      const at = items.indexOf(it);
      const base = items.slice(0, at).reduce((len, x) => len + x.str.length + 1, 0);
      const inItem = it.str.lastIndexOf(query, k) >= 0 ? it.str.lastIndexOf(query, k) : Math.max(0, it.str.indexOf(query));
      const context = sentenceAround(items.map((x) => x.str).join(" "), base + inItem, base + inItem + query.length);
      const place = workId ? { work_id: workId, file_id: fileId, page: n, context } : undefined;
      onWord({ query, place, rect: { left: clientX - 20, top: clientY - h / 2, bottom: clientY + h / 2, width: 40 } });
      return;
    }
    onMiss?.();
  };
  return (
    <div className="pdf-page-wrap" data-pickable={onPick ? "" : undefined} onClick={click} onDoubleClick={onWord ? (e) => void dblclick(e) : undefined} style={css ? { width: css.w, height: css.h } : undefined}>
      <canvas ref={canvas} className="pages-page" role="img" aria-label={`Page ${n}`} style={css ? { width: css.w, height: css.h } : undefined} />
      {css && marks.length > 0 && (
        <div className="pdf-ra-marks" aria-hidden="true" ref={markLayer}>
          {marks.map((b, i) => {
            const r = toRect(css.t, b);
            return <span key={i} className="pdf-ra-mark" style={{ left: r.left, top: r.top, width: r.width, height: r.height }} />;
          })}
        </div>
      )}
      {found.length > 0 && (
        <div className="pages-found" aria-hidden="true">
          {found.map((m, i) => (
            <mark key={i} style={{ left: m.x, top: m.y, width: m.w, height: m.h }} />
          ))}
        </div>
      )}
      {found.length > 0 && <span className="sr-only" role="status">Search match marked on page {n}</span>}
    </div>
  );
}

function ComicPage({ fileId, n, count, fit, zoom, stage }: PageProps & { fileId: number }) {
  const [nat, setNat] = useState<{ w: number; h: number } | null>(null);
  const scale = nat ? fitScale(fit, nat.w * count, nat.h, stage.w - 8, stage.h - 8, zoom) : 0;
  return (
    <img
      className="pages-page"
      src={`/api/files/${fileId}/pages/${n}`}
      alt={`Page ${n}`}
      decoding="async"
      onLoad={(e) => setNat({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
      style={nat ? { width: nat.w * scale, height: nat.h * scale } : { visibility: "hidden" }}
    />
  );
}
