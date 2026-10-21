// Page viewer for PDFs (pdf.js, rendered to canvases) and CBZ comics (the page images the server
// serves after checking the archive). Page navigation, fit width / fit page, zoom, and a
// two-page spread (right to left for manga). Progress is a page number, saved to the same
// KOReader record the e-reader syncs (PUT /api/files/:id/progress), with a hand-off prompt
// instead of a silent jump when another device is further along.
//
// pdf.js runs here without its viewer layer: only the canvas renderer, with no annotation or
// form layer and no scripting sandbox (pdf.sandbox is never loaded), so nothing in a PDF runs.
// pdf.js 6 has no eval-based code path, and the CSP keeps scripts same-origin.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeft, Chevron } from "../components/Icons";
import { ApiError, get, put } from "../lib/api";
import { relativeTime } from "../lib/format";
import { useLocalStorage } from "../lib/hooks";
import { clampPage, type Fit, fitScale, pageLabel, spreadOf, stepZoom, turn } from "../lib/pages";
import { useStore } from "../lib/store";
import type { PagesInfo } from "../lib/types";

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
interface PdfPage {
  getViewport(o: { scale: number }): { width: number; height: number };
  render(o: { canvas: HTMLCanvasElement; canvasContext: CanvasRenderingContext2D; viewport: unknown; transform?: number[] }): { promise: Promise<void>; cancel(): void };
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
  });
  return task as unknown as PdfTask;
}

export function PageReader({ workId, fileId }: { workId: number; fileId: number }) {
  const nav = useNavigate();
  const { deviceName, now, resolvedTheme } = useStore();
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
        const i = await get<PagesInfo>(`/api/files/${fileId}/pages`);
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
        const start = clampPage(mark?.page ?? server?.page ?? 1, n);
        setPage(start);
        if (mark && server?.page && server.page !== start && server.updated_at && (!mark.seenAt || server.updated_at > mark.seenAt)) setHandoff(server);
        if (!mark && server?.updated_at) writeMark(fileId, { page: start, seenAt: server.updated_at });
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
      const r = await put<NonNullable<PagesInfo["progress"]>>(`/api/files/${fileId}/progress`, { page: p, pages: n, device: deviceName }, keepalive);
      writeMark(fileId, { page: p, seenAt: r.updated_at });
    } catch {
      writeMark(fileId, { page: p, seenAt: readMark(fileId)?.seenAt ?? null });
    }
  }, [fileId, deviceName]);

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

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (document.querySelector('[aria-modal="true"]') || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t?.closest?.("input, select, textarea")) return;
      if (e.key === "Escape") return nav(`/app/library/${workId}`);
      const map: Record<string, () => void> = {
        ArrowLeft: left, ArrowRight: right, PageUp: back, PageDown: forward, " ": forward,
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
  const theme = resolvedTheme === "dark" ? "dark" : "light";
  const style = { "--reader-bg": theme === "dark" ? "#1b1712" : "#f3efe7", "--reader-ink": theme === "dark" ? "#e6dccb" : "#221d17" } as React.CSSProperties;

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

      <div className="reader-stage">
        {!loading && !error && total > 0 && (
          <>
            <button className="page-zone left" aria-label={rtl ? "Next page" : "Previous page"} onClick={left}><Chevron /></button>
            <button className="page-zone right" aria-label={rtl ? "Previous page" : "Next page"} onClick={right}><Chevron /></button>
          </>
        )}
        <div className="pages-stage" ref={stage} onDoubleClick={() => setChrome((c) => !c)}>
          {!loading && !error && total > 0 && (
            <div className="pages-spread" data-count={display.length} dir="ltr">
              {display.map((n) =>
                info?.url ? (
                  <PdfCanvas key={n} doc={pdf.current!} n={n} count={display.length} fit={view.fit} zoom={view.zoom} stage={size} />
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

function PdfCanvas({ doc, n, count, fit, zoom, stage }: PageProps & { doc: PdfDoc }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [css, setCss] = useState<{ w: number; h: number } | null>(null);
  useEffect(() => {
    let cancelled = false;
    let task: { cancel(): void } | null = null;
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
      setCss({ w: vp.width, h: vp.height });
      const r = page.render({ canvas: c, canvasContext: c.getContext("2d")!, viewport: vp, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined });
      task = r;
      await r.promise.catch(() => {});
    })().catch(() => {});
    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [doc, n, count, fit, zoom, stage.w, stage.h]);
  return <canvas ref={canvas} className="pages-page" role="img" aria-label={`Page ${n}`} style={css ? { width: css.w, height: css.h } : undefined} />;
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
