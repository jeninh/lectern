import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, post } from "../lib/api";
import { bytes } from "../lib/format";
import { PDF, READALONG_NEEDS_EPUB, hasEpub, readerPath } from "../lib/formats";
import { alignmentHint, buildReadAloud, readAloudStatus, retryAlignment } from "../lib/readalong";
import { useStore } from "../lib/store";
import type { PdfTextState, ReadAloudStatus, Work } from "../lib/types";
import { Book, Download, Headphones, Sync } from "./Icons";

const POLL_MS = 1500;
const ALIGN_POLL_MS = 5000;

/** Copy for the four kinds of book (docs/pdf-read-along.md). */
export const COPY = {
  textPdf: "Text PDF: read-along highlights the narration on its pages",
  scanned: "Scanned PDF: needs OCR. Its pages are pictures of text with no text layer, so there is nothing to align or narrate yet. Text recognition is planned.",
  comic: "Comics have pictures, not text: read-along and narration need an EPUB or a text PDF of the book.",
  exportNeedsEpub: "Narrated EPUB export needs an EPUB edition. This book's read-along is on its PDF pages.",
  reading: "Reading the PDF's text…",
} as const;

/** A comic (or another book with no text): read-along, alignment and narration have nothing to read. */
export function NeedsEpub() {
  return (
    <li className="slot" aria-disabled="true" data-state="needs-epub">
      <Sync />
      <span>
        <strong>Read-along</strong>{" "}
        <span className="hint">{COPY.comic}</span>
      </span>
      <span />
    </li>
  );
}

/**
 * Read-along rows for a work's Formats list: open the web read-along, and the EPUB 3 read-aloud
 * export with its real state (unavailable, not built, building, ready, failed). Loads only
 * while `active` (an expanded card or an open sheet), and polls only while building.
 */
export function ReadAloudFormats({ work, active }: { work: Work; active: boolean }) {
  const pdf = work.files.some((f) => f.media_type === PDF);
  if (!hasEpub(work) && !pdf && work.files.some((f) => f.kind === "ebook")) return <NeedsEpub />;
  return <ReadAloudRows work={work} active={active} />;
}

/** The text PDF whose pages read-along runs on, or the PDF states to explain why not. */
function pdfStory(st: ReadAloudStatus): { kind: "text" | "scanned" | "reading" | "failed" | null; pdf: PdfTextState | null } {
  const pdfs = st.pdfs ?? [];
  if (st.text_source === "pdf") return { kind: "text", pdf: pdfs.find((p) => p.readalong) ?? pdfs.find((p) => p.status === "ready") ?? null };
  if (st.text_source === "epub" || !pdfs.length) return { kind: null, pdf: null };
  const of = (s: PdfTextState["status"]) => pdfs.find((p) => p.status === s) ?? null;
  if (of("pending")) return { kind: "reading", pdf: of("pending") };
  if (of("needs_ocr")) return { kind: "scanned", pdf: of("needs_ocr") };
  return { kind: "failed", pdf: of("failed") };
}

function ReadAloudRows({ work, active }: { work: Work; active: boolean }) {
  const { canUpload } = useStore();
  const [st, setSt] = useState<ReadAloudStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setSt(await readAloudStatus(work.id));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [work.id]);

  useEffect(() => {
    if (active) refresh();
  }, [active, refresh]);

  const aligning = st?.alignment?.job?.status === "queued" || st?.alignment?.job?.status === "leased";
  const reading = !!st?.pdfs?.some((p) => p.status === "pending");
  useEffect(() => {
    if (!active || (st?.status !== "generating" && !aligning && !reading)) return;
    const id = window.setTimeout(refresh, st?.status === "generating" ? POLL_MS : ALIGN_POLL_MS);
    return () => window.clearTimeout(id);
  }, [active, st, aligning, reading, refresh]);

  const retry = async () => {
    try {
      const model = alignmentHint(st?.alignment)?.retryModel;
      if (model) await retryAlignment(work.id, model);
      await refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not retry the alignment");
    }
  };

  const build = async () => {
    try {
      setSt(await buildReadAloud(work.id));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not start the build");
    }
  };

  if (!st) {
    return (
      <li className="slot" aria-busy={!error}>
        <Sync />
        <span><strong>Read-along</strong> <span className="hint">{error ?? "Checking…"}</span></span>
        <span />
      </li>
    );
  }

  const story = pdfStory(st);
  if (story.kind && story.kind !== "text") {
    const p = story.pdf;
    const again = async () => {
      if (!p) return;
      try {
        await post(`/api/files/${p.file_id}/text/retry`, {});
        await refresh();
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "Could not retry");
      }
    };
    return (
      <li className="slot" data-unavailable="" data-state={`pdf-${story.kind}`} aria-disabled={story.kind === "failed" && canUpload ? undefined : "true"}>
        <Sync />
        <span>
          <strong>Read-along</strong>{" "}
          <span className="hint" aria-live="polite">
            {story.kind === "scanned" && COPY.scanned}
            {story.kind === "reading" && COPY.reading}
            {story.kind === "failed" && `Could not read the PDF's text${p?.error ? ` (${p.error})` : ""}.`}
            {error && ` · ${error}`}
          </span>
        </span>
        {story.kind === "failed" && canUpload ? <button className="btn sm ghost" onClick={again}>Retry</button> : story.kind === "reading" ? <span className="spinner" role="img" aria-label="Reading" /> : <span />}
      </li>
    );
  }

  if (story.kind === "text") {
    const align = alignmentHint(st.alignment);
    const file = work.files.find((f) => f.id === story.pdf?.file_id) ?? work.files.find((f) => f.media_type === PDF);
    const hint = st.readalong
      ? `${COPY.textPdf}${story.pdf && story.pdf.pages ? ` · ${story.pdf.pages} pages` : ""}`
      : !work.audio ? `${COPY.textPdf}, once it has an audiobook: make one, or add the audiobook files`
      : align ? align.text
      : `${COPY.textPdf}, once the audiobook is aligned to it`;
    return (
      <>
        <li className="slot" data-state="pdf-text" aria-disabled={st.readalong ? undefined : "true"}>
          {st.readalong ? <Headphones /> : <Sync />}
          <span>
            <strong>Read-along</strong>{" "}
            <span className="hint" aria-live="polite">{hint}{error && ` · ${error}`}</span>
          </span>
          {st.readalong && file ? (
            <span className="row" style={{ gap: 4, flexWrap: "nowrap" }}>
              <Link className="btn sm ghost" to={readerPath(work, file)} aria-label={`Read along on the PDF pages of ${work.title}`}>Pages</Link>
              <Link className="btn sm ghost" to={`/app/read/${work.id}`} aria-label={`Read along with the text of ${work.title}`}>Text</Link>
            </span>
          ) : <span />}
        </li>
        <li className="slot" data-unavailable="" aria-disabled="true" data-state="export-needs-epub">
          <Book />
          <span><strong>Narrated EPUB</strong> <span className="hint">{COPY.exportNeedsEpub}</span></span>
          <span />
        </li>
      </>
    );
  }

  if (st.status === "unavailable") {
    const align = alignmentHint(st.alignment);
    const hint =
      st.reason === "needs_epub" ? READALONG_NEEDS_EPUB :
      st.reason === "no_audio" ? "Needs a linked audiobook" :
      st.reason === "missing_file" ? "A file is missing from the library folder" :
      align ? align.text :
      st.reason === "alignment_stale" ? "Audio and text changed; waiting for re-alignment" :
      "Needs an audiobook aligned to this EPUB";
    const showAlign = align && hint === align.text;
    const canRetry = Boolean(showAlign && align.retryModel && canUpload);
    return (
      // aria-disabled would also disable the Retry button for assistive technology
      <li className="slot" data-unavailable="" aria-disabled={canRetry ? undefined : "true"}>
        <Sync />
        <span>
          <strong>Read-along</strong>{" "}
          <span className="hint" aria-live="polite" title={showAlign ? align.detail ?? undefined : undefined}>
            {hint}
            {showAlign && align.docsUrl && <> · <a href={align.docsUrl} target="_blank" rel="noreferrer">How to turn on alignment</a></>}
            {error && ` · ${error}`}
          </span>
          {showAlign && align.progress != null && (
            <span className="progress-bar" role="progressbar" aria-label="Alignment progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.floor(align.progress * 100)}>
              <span style={{ width: `${align.progress * 100}%` }} />
            </span>
          )}
        </span>
        {canRetry && align ? (
          <button className="btn sm ghost" onClick={retry} aria-label={`${align.retryLabel} aligning ${work.title}`}>{align.retryLabel}</button>
        ) : <span />}
      </li>
    );
  }

  const coverage = st.alignment?.coverage;
  return (
    <>
      <li className="slot">
        <Headphones />
        <span>
          <strong>Read-along</strong>{" "}
          <span className="hint">Listen with highlighting{coverage != null && coverage < 0.999 ? ` · ${Math.round(coverage * 100)}% aligned` : ""}</span>
        </span>
        <Link className="btn sm ghost" to={`/app/read/${work.id}`} aria-label={`Open read-along for ${work.title}`}>Open</Link>
      </li>
      <li className="slot" data-state={st.status} title="EPUB 3 with Media Overlays: the text highlights as the narration plays in readers that support it">
        <Book />
        <span>
          <strong>Narrated EPUB</strong>{" "}
          <span className="hint" aria-live="polite">
            {st.status === "ready" && st.size != null && `${bytes(st.size)} · EPUB 3`}
            {st.status === "not_generated" && "EPUB 3, built on request"}
            {st.status === "generating" && "Building…"}
            {st.status === "failed" && (st.message ?? "Build failed")}
            {error && ` · ${error}`}
          </span>
        </span>
        {st.status === "ready" && st.download_url ? (
          <a className="btn sm ghost" href={st.download_url} aria-label={`Download ${work.title} with narration (EPUB 3)`} title="Download"><Download /></a>
        ) : st.status === "generating" ? (
          <span className="spinner" role="img" aria-label="Building" />
        ) : (
          <button className="btn sm ghost" onClick={build} aria-label={`${st.status === "failed" ? "Retry building" : "Prepare"} the narrated EPUB of ${work.title}`}>
            {st.status === "failed" ? "Retry" : "Prepare"}
          </button>
        )}
      </li>
    </>
  );
}
