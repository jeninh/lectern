import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../lib/api";
import {
  firstPreviewPage,
  formatPages,
  numberingSummary,
  parsePages,
  pdfAnalysis,
  previewDoc,
  previewImageUrl,
  previewPdfEpub,
  ruleSummary,
  type PdfAnalysis,
  type PdfRules,
  type PreviewPage,
} from "../lib/pdf2epub";
import { Sheet } from "./Sheet";

/** A page-list input ("1-3, 7") that keeps its text while invalid and reports the problem. */
function PagesInput({ label, value, max, onChange, hint }: { label: string; value: number[]; max: number; onChange: (v: number[]) => void; hint?: string }) {
  const [text, setText] = useState(formatPages(value));
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setText(formatPages(value));
  }, [value]);
  return (
    <label className="field">
      <span>{label}</span>
      <input
        className="input"
        value={text}
        placeholder="e.g. 1-3, 7"
        aria-invalid={!!error}
        onChange={(e) => {
          setText(e.target.value);
          try {
            onChange(parsePages(e.target.value, max));
            setError(null);
          } catch (err) {
            setError((err as Error).message);
          }
        }}
      />
      {error ? <small className="field-error">{error}</small> : hint && <small className="hint">{hint}</small>}
    </label>
  );
}

/**
 * The review step of PDF → EPUB (analyse → review rules → convert): every detected rule in plain
 * language with a control to change it, and a side-by-side preview of sample pages (the PDF page
 * beside its converted text) that follows the rules as they are edited.
 */
export function PdfReviewSheet({ workId, title, open, onClose, onConvert, reconvert }: {
  workId: number;
  title: string;
  open: boolean;
  onClose: () => void;
  onConvert: (rules: PdfRules) => Promise<void>;
  reconvert: boolean;
}) {
  const [a, setA] = useState<PdfAnalysis | null>(null);
  const [rules, setRules] = useState<PdfRules | null>(null);
  const [page, setPage] = useState<number | null>(null);
  const [shown, setShown] = useState<PreviewPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    pdfAnalysis(workId)
      .then((x) => {
        setA(x);
        setRules(x.rules);
        setPage(firstPreviewPage(x));
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load the analysis"));
  }, [open, workId]);

  // The converted side follows the rules (debounced: typing a page range should not flood).
  useEffect(() => {
    if (!rules || page == null) return;
    const id = window.setTimeout(() => {
      previewPdfEpub(workId, [page], rules)
        .then((r) => setShown(r.pages[0] ?? null))
        .catch(() => setShown(null));
    }, 250);
    return () => window.clearTimeout(id);
  }, [workId, rules, page]);

  const set = (patch: Partial<PdfRules>) => setRules((r) => (r ? { ...r, ...patch } : r));
  const chapterRule = (index: number) => rules?.chapters.find((c) => c.index === index) ?? { index, title: null, merge: false };
  const setChapter = (index: number, patch: Partial<PdfRules["chapters"][number]>) =>
    setRules((r) => {
      if (!r) return r;
      const next = { ...chapterRule(index), ...patch };
      const rest = r.chapters.filter((c) => c.index !== index);
      return { ...r, chapters: next.title || next.merge ? [...rest, next].sort((x, y) => x.index - y.index) : rest };
    });
  const summary = useMemo(() => (a && rules ? ruleSummary(a, rules) : []), [a, rules]);
  const conf = (p: number) => a?.pages[p - 1];

  if (!open) return null;
  const max = a?.summary.pages ?? 1;
  const footer = (
    <>
      <button className="btn ghost" onClick={onClose}>Cancel</button>
      <button
        className="btn primary"
        disabled={!rules || busy}
        onClick={async () => {
          if (!rules) return;
          setBusy(true);
          try {
            await onConvert(rules);
          } catch (e) {
            setError(e instanceof ApiError ? e.message : "Could not start the conversion");
          } finally {
            setBusy(false);
          }
        }}
      >
        {reconvert ? "Re-convert with these rules" : "Convert with these rules"}
      </button>
    </>
  );

  return (
    <Sheet open={open} onClose={onClose} title={`Convert ${title} to EPUB`} wide footer={footer} labelledBy="pdf-review-title">
      <div className="pdf-review">
        {error && <p className="field-error" role="alert">{error}</p>}
        {!a || !rules ? (
          !error && <p className="hint">Loading the analysis…</p>
        ) : (
          <>
            <section aria-labelledby="pdf-review-summary">
              <h3 id="pdf-review-summary">How the PDF will be converted</h3>
              <ul className="rule-summary">
                {summary.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </section>

            <section className="pdf-preview" aria-labelledby="pdf-review-preview">
              <h3 id="pdf-review-preview">Preview</h3>
              <div className="preview-pages" role="group" aria-label="Sample pages">
                {a.preview_pages.map((p) => (
                  <button
                    key={p}
                    className={`chip${conf(p)?.flagged ? " warn" : ""}`}
                    aria-pressed={p === page}
                    title={conf(p)?.flagged ? `Low confidence: ${conf(p)?.reasons.join(", ")}` : undefined}
                    onClick={() => setPage(p)}
                  >
                    {p}
                  </button>
                ))}
              </div>
              {page != null && (
                <div className="side-by-side">
                  <figure>
                    <img src={previewImageUrl(workId, page)} alt={`PDF page ${page}`} loading="lazy" />
                    <figcaption>PDF page {page}{shown ? ` (printed ${shown.label})` : ""}</figcaption>
                  </figure>
                  <figure>
                    <iframe title={`Converted text of page ${page}`} sandbox="" srcDoc={previewDoc(shown?.html ?? "")} />
                    <figcaption>
                      Converted
                      {shown?.confidence != null && ` · confidence ${Math.round(shown.confidence * 100)}%`}
                      {shown?.as_image && " · kept as an image"}
                      {shown?.dropped && " · left out"}
                      {shown && shown.reasons.length > 0 && ` · ${shown.reasons.join(", ")}`}
                    </figcaption>
                  </figure>
                </div>
              )}
              {page != null && (
                <label className="check">
                  <input
                    type="checkbox"
                    checked={rules.page_images.includes(page)}
                    onChange={(e) => set({ page_images: e.target.checked ? [...rules.page_images, page].sort((x, y) => x - y) : rules.page_images.filter((p) => p !== page) })}
                  />{" "}
                  Keep page {page} as an image
                </label>
              )}
            </section>

            <section aria-labelledby="pdf-review-pages">
              <h3 id="pdf-review-pages">Page numbers</h3>
              <p className="hint">
                {numberingSummary(a)}. {a.running_heads_removed} running header and footer lines removed
                {a.columns_pages ? `; ${a.columns_pages} ${a.columns_pages === 1 ? "page" : "pages"} read as columns` : ""}.
              </p>
              <label className="check">
                <input type="checkbox" checked={rules.page_numbers.keep} onChange={(e) => set({ page_numbers: { ...rules.page_numbers, keep: e.target.checked } })} /> Keep the PDF's page numbers as reference pages
              </label>
              <label className="field">
                <span>Offset added to numbered pages</span>
                <input className="input" type="number" min={-10000} max={10000} value={rules.page_numbers.offset} disabled={!rules.page_numbers.keep} onChange={(e) => set({ page_numbers: { ...rules.page_numbers, offset: Number(e.target.value) || 0 } })} />
              </label>
            </section>

            <section aria-labelledby="pdf-review-chapters">
              <h3 id="pdf-review-chapters">Chapters</h3>
              <ol className="chapter-rules">
                {a.chapters.map((c) => {
                  const r = chapterRule(c.index);
                  return (
                    <li key={c.index}>
                      <input className="input" aria-label={`Title of chapter ${c.index + 1}`} value={r.title ?? c.title} onChange={(e) => setChapter(c.index, { title: e.target.value === c.title ? null : e.target.value })} />
                      <span className="hint">pp. {c.page_start}–{c.page_end}</span>
                      {c.index > 0 ? (
                        <label className="check">
                          <input type="checkbox" checked={r.merge} onChange={(e) => setChapter(c.index, { merge: e.target.checked })} /> Join to previous
                        </label>
                      ) : (
                        <span />
                      )}
                    </li>
                  );
                })}
              </ol>
              {a.headings.length > 0 && (
                <p className="hint">Headings: {a.headings.map((h) => `${h.size} pt → level ${h.level - 1} (e.g. “${h.example}”)`).join("; ")}</p>
              )}
            </section>

            <section aria-labelledby="pdf-review-content">
              <h3 id="pdf-review-content">Notes, pictures, and verse</h3>
              <label className="field">
                <span>Footnotes ({a.summary.footnotes} found)</span>
                <select className="select" value={rules.footnotes} onChange={(e) => set({ footnotes: e.target.value as PdfRules["footnotes"] })}>
                  <option value="end">At the end of each chapter</option>
                  <option value="popup">After their paragraph (pop-ups)</option>
                  <option value="inline">Leave as text</option>
                </select>
              </label>
              <label className="field">
                <span>Figures ({a.summary.figures} found)</span>
                <select className="select" value={rules.figures} onChange={(e) => set({ figures: e.target.value as PdfRules["figures"] })}>
                  <option value="keep">Keep as images with captions</option>
                  <option value="drop">Leave out (keep captions)</option>
                </select>
              </label>
              <label className="field">
                <span>Tables ({a.summary.tables} found, {a.summary.complex_tables} complex)</span>
                <select className="select" value={rules.tables} onChange={(e) => set({ tables: e.target.value as PdfRules["tables"] })}>
                  <option value="auto">Simple ones as tables, complex ones as images</option>
                  <option value="image">All as images</option>
                  <option value="text">As plain text</option>
                </select>
              </label>
              <label className="field">
                <span>Maths ({a.summary.maths} found)</span>
                <select className="select" value={rules.maths} onChange={(e) => set({ maths: e.target.value as PdfRules["maths"] })}>
                  <option value="image">As images, text as alt</option>
                  <option value="text">As text</option>
                </select>
              </label>
              <label className="field">
                <span>Verse</span>
                <select className="select" value={rules.verse} onChange={(e) => set({ verse: e.target.value as PdfRules["verse"] })}>
                  <option value="auto">Detect automatically</option>
                  <option value="off">Only on the pages below</option>
                </select>
              </label>
              <PagesInput label="Verse on these pages" value={rules.verse_pages} max={max} onChange={(v) => set({ verse_pages: v })} hint="Line breaks are kept as printed." />
              <label className="check">
                <input type="checkbox" checked={rules.styles} onChange={(e) => set({ styles: e.target.checked })} /> Keep italic, bold, small caps, and superscripts
              </label>
            </section>

            <section aria-labelledby="pdf-review-images">
              <h3 id="pdf-review-images">Pages kept as images or left out</h3>
              {a.summary.flagged_pages.length > 0 && (
                <p className="hint">
                  Low confidence: {a.summary.flagged_pages.map((p) => `${p} (${conf(p)?.reasons.join(", ")})`).join("; ")}. These start as images.
                </p>
              )}
              <PagesInput label="Keep as page images" value={rules.page_images} max={max} onChange={(v) => set({ page_images: v })} />
              {a.matter.length > 0 && (
                <p className="hint">Found: {a.matter.map((m) => `${m.kind} on page ${m.page}`).join(", ")}.</p>
              )}
              <PagesInput label="Leave out these pages" value={rules.drop_pages} max={max} onChange={(v) => set({ drop_pages: v })} hint="For example a printed cover, copyright page, or index." />
              <button className="btn sm ghost" onClick={() => setRules(a.defaults)}>Reset to the detected rules</button>
            </section>
          </>
        )}
      </div>
    </Sheet>
  );
}
