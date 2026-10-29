// The cover tab's pickers (issue #85): generated designs in a keyboard-friendly grid, covers taken
// from the book's own files (an upload, an EPUB image, a PDF or comic page), a fixed 2:3 crop with
// live card and thumbnail previews, and the history of earlier covers with one-click revert.
// Every choice only changes the editor's draft; nothing is saved until the editor's Save.
//
// The server renders nothing here but the generated designs. PDF pages are drawn in the browser
// with pdf.js (the same file the page viewer opens) and comic pages are the reader's own images.

import { useCallback, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { get, post, postBlob } from "../lib/api";
import {
  CANDIDATE_PAGE,
  DEFAULT_FONT,
  PREVIEW_SIZES,
  candidateDraft,
  createCandidateLoader,
  cropTooSmall,
  imageTooSmall,
  initialBox,
  isWholeImage,
  maxBoxWidth,
  minBoxWidth,
  moveBox,
  normaliseCrop,
  previewLayout,
  radioKeyTarget,
  resizeBox,
  revertDraft,
  sameDesign,
  selectedIndex,
  type Box,
  type Crop,
} from "../lib/covers";
import type { CoverDraft } from "../lib/editor";
import { relativeTime } from "../lib/format";
import type { CoverCandidate, CoverFont, CoverHistoryEntry, EditorFile, PagesInfo, StagedCover } from "../lib/types";
import { Undo, Upload } from "./Icons";

export type Toast = (message: string, kind?: "error" | "info") => void;

const FONT_LABEL: Record<CoverFont, string> = { display: "Display", text: "Text", noto: "Wide alphabets" };
const PAGE = 12;

export interface CoverPickerProps {
  workId: number;
  /** The title and author line the generated designs show (the draft's, so what Save will use). */
  title: string;
  author: string;
  cover: CoverDraft | null;
  /** The font the book's generated cover has now. */
  savedFont?: CoverFont;
  files: EditorFile[];
  /** Bumps after a save so the history list reloads. */
  version: number;
  onCover: (c: CoverDraft | null) => void;
  toast: Toast;
}

export function CoverPicker({ workId, title, author, cover, savedFont, files, version, onCover, toast }: CoverPickerProps) {
  const [staged, setStaged] = useState<StagedCover | null>(null);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const stage = useCallback(
    async (body: Record<string, unknown>) => {
      setBusy(true);
      try {
        setStaged(await post<StagedCover>(`/api/works/${workId}/cover/stage`, body));
      } catch (e) {
        toast((e as Error).message, "error");
      } finally {
        setBusy(false);
      }
    },
    [workId, toast],
  );
  const upload = async (f: File) => {
    if (f.size > 10 * 1024 * 1024) return toast("That image is too large for a cover (10 MB at most).", "error");
    setBusy(true);
    try {
      const up = await postBlob<{ token: string }>(`/api/works/${workId}/cover`, f);
      setStaged(await post<StagedCover>(`/api/works/${workId}/cover/stage`, { source: "staged", token: up.token }));
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  const useCrop = async (crop: Crop | null) => {
    if (!staged) return;
    setBusy(true);
    try {
      const next = crop && !isWholeImage(crop) ? await post<StagedCover>(`/api/works/${workId}/cover/stage`, { source: "staged", token: staged.token, crop }) : staged;
      onCover({ action: "upload", token: next.token, preview: next.preview_url });
      setStaged(null);
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  const epubs = files.filter((f) => f.format === "EPUB");
  const paged = files.filter((f) => f.format !== "EPUB");
  return (
    <div className="cover-picker stack" style={{ gap: 16 }}>
      <section aria-labelledby="cp-gen">
        <h3 id="cp-gen" className="detail-h">Generated covers</h3>
        <CandidateGrid workId={workId} title={title} author={author} cover={cover} savedFont={savedFont} onPick={onCover} />
      </section>

      <section aria-labelledby="cp-src">
        <h3 id="cp-src" className="detail-h">An image</h3>
        {staged ? (
          <CropEditor key={staged.token} staged={staged} busy={busy} onApply={useCrop} onCancel={() => setStaged(null)} />
        ) : (
          <div className="stack" style={{ gap: 8 }}>
            <input ref={input} type="file" accept="image/jpeg,image/png,image/webp" hidden onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) void upload(f); }} />
            <button type="button" className="btn" style={{ justifySelf: "start" }} onClick={() => input.current?.click()} disabled={busy} aria-busy={busy}>
              <Upload /> {busy ? "Working…" : "Upload an image"}
            </button>
            <span className="hint">JPEG, PNG, or WebP up to 10 MB. You can crop it next.</span>
            {epubs.map((f) => (
              <EpubImages key={f.id} workId={workId} file={f} disabled={busy} onPick={(index) => stage({ source: "epub_image", file_id: f.id, index })} />
            ))}
            {paged.map((f) => (
              <PageSource key={f.id} file={f} disabled={busy} onPick={(page) => stage({ source: "page", file_id: f.id, page })} />
            ))}
          </div>
        )}
      </section>

      <CoverHistory key={version} workId={workId} draft={cover} onRevert={onCover} />
    </div>
  );
}

// --- generated candidates ------------------------------------------------------------------------

export function CandidateGrid({ workId, title, author, cover, savedFont, onPick }: { workId: number; title: string; author: string; cover: CoverDraft | null; savedFont?: CoverFont; onPick: (c: CoverDraft) => void }) {
  const [font, setFont] = useState<CoverFont>(cover?.action === "generate" && cover.font ? cover.font : savedFont ?? DEFAULT_FONT);
  const [fonts, setFonts] = useState<CoverFont[]>([]);
  const [list, setList] = useState<CoverCandidate[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const next = useRef(0);
  const loader = useRef<ReturnType<typeof createCandidateLoader> | null>(null);
  const radios = useRef<(HTMLButtonElement | null)[]>([]);
  const first = useRef(true);

  useEffect(() => {
    loader.current = createCandidateLoader({
      onBusy: setBusy,
      onError: setError,
      onResult: (r, q) => {
        setError(null);
        setFonts(r.fonts);
        next.current = q.offset + r.candidates.length;
        setList((old) => (q.offset === 0 ? r.candidates : [...old, ...r.candidates.filter((c) => !old.some((o) => sameDesign(o, c)))]));
      },
    });
    return () => loader.current?.cancel();
  }, []);
  // Typing settles for 400 ms before a request; the first load and a font change go at once.
  useEffect(() => {
    const q = { workId, title, author, font, offset: 0 };
    if (first.current) loader.current?.now(q);
    else loader.current?.schedule(q);
    first.current = false;
  }, [workId, title, author, font]);

  const sel = selectedIndex(list, cover);
  const stop = sel >= 0 ? sel : 0;
  const key = (e: KeyboardEvent, i: number) => {
    const to = radioKeyTarget(e.key, i, list.length);
    if (to === null) return;
    e.preventDefault();
    radios.current[to]?.focus();
    onPick(candidateDraft(list[to]));
  };
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="gen-controls">
        <label className="row" style={{ gap: 6 }}>
          <span className="hint">Font</span>
          <select className="select sm" value={font} onChange={(e) => setFont(e.target.value as CoverFont)}>
            {(fonts.length ? fonts : [font]).map((f) => <option key={f} value={f}>{FONT_LABEL[f] ?? f}</option>)}
          </select>
        </label>
        <span className="hint">Uses the title and authors from Details.</span>
      </div>
      {error && <div className="alert error" role="alert">{error}</div>}
      <div className="cand-grid" role="radiogroup" aria-label="Generated cover designs" aria-busy={busy}>
        {list.map((c, i) => (
          <button
            key={`${c.style}-${c.seed}-${c.font}`}
            ref={(el) => { radios.current[i] = el; }}
            type="button"
            role="radio"
            aria-checked={sel === i}
            aria-label={`${c.palette}, ${c.style} style`}
            tabIndex={i === stop ? 0 : -1}
            className="cand"
            onClick={() => onPick(candidateDraft(c))}
            onKeyDown={(e) => key(e, i)}
          >
            <img src={c.url} alt="" loading="lazy" decoding="async" width={120} height={180} />
            <span className="hint">{c.palette}</span>
          </button>
        ))}
      </div>
      <button type="button" className="btn sm ghost" style={{ justifySelf: "start" }} disabled={busy || !list.length || next.current >= 1_000_000 - CANDIDATE_PAGE} onClick={() => loader.current?.now({ workId, title, author, font, offset: next.current })}>
        More colours
      </button>
    </div>
  );
}

// --- sources ---------------------------------------------------------------------------------------

function EpubImages({ workId, file, disabled, onPick }: { workId: number; file: EditorFile; disabled: boolean; onPick: (index: number) => void }) {
  const [open, setOpen] = useState(false);
  const [shown, setShown] = useState(PAGE);
  const [broken, setBroken] = useState<Set<number>>(new Set());
  const name = file.rel_path.split("/").pop();
  const idx = Array.from({ length: shown }, (_, i) => i).filter((i) => !broken.has(i));
  // The server lists images in order and 404s past the last, so a whole page that fails is the end.
  const ended = Array.from({ length: PAGE }, (_, k) => shown - 1 - k).every((i) => broken.has(i)) || shown >= 200;
  return (
    <details className="src-group" onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>Images in {name}</summary>
      {open && (
        <>
          <div className="thumb-grid">
            {idx.map((i) => (
              <button key={i} type="button" className="thumb" disabled={disabled} aria-label={`Use image ${i + 1} from ${name}`} onClick={() => onPick(i)}>
                <img src={`/api/works/${workId}/cover/sources/epub/${file.id}/${i}.jpg`} alt="" loading="lazy" decoding="async" onError={() => setBroken((b) => new Set(b).add(i))} />
              </button>
            ))}
          </div>
          {idx.length === 0 && ended && <p className="hint">No usable images in this EPUB.</p>}
          {!ended && <button type="button" className="btn sm ghost" onClick={() => setShown((s) => s + PAGE)}>More images</button>}
        </>
      )}
    </details>
  );
}

interface PdfPage {
  getViewport(o: { scale: number }): { width: number; height: number };
  render(o: { canvas: HTMLCanvasElement; canvasContext: CanvasRenderingContext2D; viewport: unknown }): { promise: Promise<void>; cancel(): void };
}
interface PdfDoc {
  numPages: number;
  getPage(n: number): Promise<PdfPage>;
}
interface PdfTask {
  promise: Promise<PdfDoc>;
  destroy(): Promise<void>;
}

/** Same pdf.js setup as the page viewer: canvas rendering only, assets served by Lectern. */
async function openPdf(url: string): Promise<PdfTask> {
  const [pdfjs, worker] = await Promise.all([import("pdfjs-dist"), import("pdfjs-dist/build/pdf.worker.min.mjs?url")]);
  pdfjs.GlobalWorkerOptions.workerSrc = worker.default;
  const assets = `${import.meta.env.BASE_URL}pdfjs/`;
  return pdfjs.getDocument({ url, withCredentials: true, cMapUrl: `${assets}cmaps/`, cMapPacked: true, standardFontDataUrl: `${assets}standard_fonts/`, wasmUrl: `${assets}wasm/`, enableXfa: false }) as unknown as PdfTask;
}

function PdfThumb({ doc, n }: { doc: PdfDoc; n: number }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    let job: { promise: Promise<void>; cancel(): void } | null = null;
    let gone = false;
    (async () => {
      const page = await doc.getPage(n);
      const el = canvas.current;
      const ctx = el?.getContext("2d");
      if (gone || !el || !ctx) return;
      const vp = page.getViewport({ scale: 140 / page.getViewport({ scale: 1 }).width });
      el.width = Math.ceil(vp.width);
      el.height = Math.ceil(vp.height);
      job = page.render({ canvas: el, canvasContext: ctx, viewport: vp });
      await job.promise;
    })().catch(() => {});
    return () => {
      gone = true;
      job?.cancel();
    };
  }, [doc, n]);
  return <canvas ref={canvas} aria-hidden="true" />;
}

function PageSource({ file, disabled, onPick }: { file: EditorFile; disabled: boolean; onPick: (page: number) => void }) {
  const [open, setOpen] = useState(false);
  const [info, setInfo] = useState<PagesInfo | null>(null);
  const [doc, setDoc] = useState<PdfDoc | null>(null);
  const [total, setTotal] = useState(0);
  const [shown, setShown] = useState(PAGE);
  const [error, setError] = useState<string | null>(null);
  const name = file.rel_path.split("/").pop();
  useEffect(() => {
    if (!open) return;
    let gone = false;
    let task: PdfTask | null = null;
    (async () => {
      try {
        const i = await get<PagesInfo>(`/api/files/${file.id}/pages`);
        if (gone) return;
        setInfo(i);
        if (i.url) {
          task = await openPdf(i.url);
          const d = await task.promise;
          if (gone) return;
          setDoc(d);
          setTotal(d.numPages);
        } else setTotal(i.pages ?? 0);
      } catch (e) {
        if (!gone) setError((e as Error).message || "Could not open this file.");
      }
    })();
    return () => {
      gone = true;
      task?.destroy().catch(() => {});
      setDoc(null);
    };
  }, [open, file.id]);
  const pages = Array.from({ length: Math.min(shown, total) }, (_, i) => i + 1);
  return (
    <details className="src-group" onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>Pages of {name}</summary>
      {open && (
        <>
          {error && <div className="alert error" role="alert">{error}</div>}
          {!error && !info && <p className="hint">Opening…</p>}
          <div className="thumb-grid">
            {pages.map((n) => (
              <button key={n} type="button" className="thumb" disabled={disabled} aria-label={`Use page ${n} of ${name}`} onClick={() => onPick(n)}>
                {info?.url ? doc && <PdfThumb doc={doc} n={n} /> : <img src={`/api/files/${file.id}/pages/${n}`} alt="" loading="lazy" decoding="async" />}
                <span className="n">{n}</span>
              </button>
            ))}
          </div>
          {total > shown && <button type="button" className="btn sm ghost" onClick={() => setShown((s) => s + PAGE)}>More pages</button>}
        </>
      )}
    </details>
  );
}

// --- crop ------------------------------------------------------------------------------------------

export function CropEditor({ staged, busy, onApply, onCancel }: { staged: StagedCover; busy: boolean; onApply: (crop: Crop | null) => void; onCancel: () => void }) {
  const { width: iw, height: ih } = staged;
  const [box, setBox] = useState<Box>(() => initialBox(iw, ih));
  const stage = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number; box: Box } | null>(null);
  const id = useId();
  const crop = useMemo(() => normaliseCrop(box, iw, ih), [box, iw, ih]);
  const small = cropTooSmall(crop, iw, ih);
  const px = (v: number) => Math.round(v);
  const down = (e: PointerEvent) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, y: e.clientY, box };
  };
  const move = (e: PointerEvent) => {
    const d = drag.current;
    const w = stage.current?.clientWidth;
    if (!d || !w) return;
    const k = iw / w;
    setBox(moveBox(d.box, (e.clientX - d.x) * k, (e.clientY - d.y) * k, iw, ih));
  };
  const key = (e: KeyboardEvent) => {
    const step = iw * (e.shiftKey ? 0.05 : 0.01);
    const to: Record<string, [number, number]> = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (to[e.key]) setBox(moveBox(box, to[e.key][0], to[e.key][1], iw, ih));
    else if (e.key === "+" || e.key === "=") setBox(resizeBox(box, box.w + iw * 0.02, iw, ih));
    else if (e.key === "-") setBox(resizeBox(box, box.w - iw * 0.02, iw, ih));
    else return;
    e.preventDefault();
  };
  const pct = (v: number, of: number) => `${(v / of) * 100}%`;
  return (
    <div className="crop-editor">
      <div className="crop-main">
        <div className="crop-stage" ref={stage} style={{ aspectRatio: `${iw} / ${ih}`, maxWidth: (420 * iw) / ih }}>
          <img src={staged.preview_url} alt="The image to crop" draggable={false} />
          <div
            className="crop-box"
            role="group"
            tabIndex={0}
            aria-label="Crop box, 2 to 3. Arrow keys move it; plus and minus resize it."
            style={{ left: pct(box.x, iw), top: pct(box.y, ih), width: pct(box.w, iw), height: pct(box.h, ih) }}
            onPointerDown={down}
            onPointerMove={move}
            onPointerUp={() => (drag.current = null)}
            onPointerCancel={() => (drag.current = null)}
            onKeyDown={key}
          />
        </div>
        <label className="row" htmlFor={`${id}-zoom`} style={{ gap: 8 }}>
          <span className="hint">Size</span>
          <input id={`${id}-zoom`} type="range" min={minBoxWidth(iw, ih)} max={maxBoxWidth(iw, ih)} step={1} value={box.w} disabled={minBoxWidth(iw, ih) >= maxBoxWidth(iw, ih)} onChange={(e) => setBox(resizeBox(box, Number(e.target.value), iw, ih))} />
        </label>
        <p className="hint" style={{ margin: 0 }} aria-live="polite">
          {small || imageTooSmall(iw, ih)
            ? `This is only ${px(box.w)} × ${px(box.h)} px of a ${iw} × ${ih} px image. Covers need at least 200 × 300 px to stay sharp.`
            : `Crop: ${px(box.w)} × ${px(box.h)} px of ${iw} × ${ih}.`}
        </p>
      </div>
      <div className="crop-previews" aria-label="How the cover will look">
        {(["card", "thumb"] as const).map((k) => (
          <figure key={k}>
            <div className="crop-preview" role="img" aria-label={k === "card" ? "Library card preview" : "Thumbnail preview"} style={{ backgroundImage: `url("${staged.preview_url}")`, ...previewLayout(crop, PREVIEW_SIZES[k].w) }} />
            <figcaption className="hint">{k === "card" ? "Library card" : "OPDS and KOReader, about 100 px"}</figcaption>
          </figure>
        ))}
      </div>
      <div className="row crop-actions">
        <button type="button" className="btn primary" disabled={busy || small} aria-busy={busy} onClick={() => onApply(crop)}>Use this crop</button>
        <button type="button" className="btn" disabled={busy} onClick={() => onApply(null)}>Use the whole image</button>
        <button type="button" className="btn ghost" disabled={busy} onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

// --- history ---------------------------------------------------------------------------------------

const SOURCE_LABEL: Record<string, string> = { upload: "uploaded", embedded: "from the book file", image: "from an image in the book", page: "from a page of the book" };

export function CoverHistory({ workId, draft, onRevert }: { workId: number; draft: CoverDraft | null; onRevert: (c: CoverDraft) => void }) {
  const [entries, setEntries] = useState<CoverHistoryEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let gone = false;
    get<{ entries: CoverHistoryEntry[] }>(`/api/works/${workId}/cover/history`)
      .then((r) => !gone && setEntries(r.entries))
      .catch((e) => !gone && setError((e as Error).message));
    return () => {
      gone = true;
    };
  }, [workId]);
  return (
    <section aria-labelledby="cp-hist">
      <h3 id="cp-hist" className="detail-h">Earlier covers</h3>
      <p className="hint" style={{ marginTop: 0 }}>Covers this book has had in the last 30 days. Older ones are cleared.</p>
      {error && <div className="alert error" role="alert">{error}</div>}
      {entries && entries.length <= 1 && <p className="hint">No earlier covers yet.</p>}
      {entries && entries.length > 0 && (
        <ul className="cover-history">
          {entries.map((e, i) => {
            const patch = revertDraft(e);
            const picked = !!patch && draft?.action === patch.action && (patch.action === "revision" ? draft.revision_id === patch.revision_id : sameDesign(draft, patch));
            const what = e.kind === "generated" ? `Generated, ${e.style ?? "automatic"} style` : `Image${e.source ? ` ${SOURCE_LABEL[e.source] ?? e.source}` : ""}`;
            return (
              <li key={`${e.revision_id ?? "now"}-${i}`} data-current={e.current || undefined}>
                <img src={e.url} alt="" loading="lazy" decoding="async" width={60} height={90} />
                <span className="hint">
                  <strong>{e.current ? "Current" : what}</strong>
                  {e.current ? ` · ${what.toLowerCase()}` : e.replaced_at ? ` · replaced ${relativeTime(e.replaced_at)}` : ""}
                </span>
                {patch && (
                  <button type="button" className="btn sm ghost" aria-pressed={picked} aria-label={`Revert to the ${what.toLowerCase()} cover${e.replaced_at ? ` replaced ${relativeTime(e.replaced_at)}` : ""}`} onClick={() => onRevert(patch)}>
                    <Undo /> {picked ? "Reverting on save" : "Revert"}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
