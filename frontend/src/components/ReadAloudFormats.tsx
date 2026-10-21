import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../lib/api";
import { bytes } from "../lib/format";
import { READALONG_NEEDS_EPUB, hasEpub } from "../lib/formats";
import { alignmentHint, buildReadAloud, readAloudStatus, retryAlignment } from "../lib/readalong";
import { useStore } from "../lib/store";
import type { ReadAloudStatus, Work } from "../lib/types";
import { Book, Download, Headphones, Sync } from "./Icons";

const POLL_MS = 1500;
const ALIGN_POLL_MS = 5000;

/** Read-along, alignment, and the narrated EPUB are built on text; PDFs and comics have none. */
export function NeedsEpub() {
  return (
    <li className="slot" aria-disabled="true" data-state="needs-epub">
      <Sync />
      <span>
        <strong>Read-along</strong>{" "}
        <span className="hint">{READALONG_NEEDS_EPUB}. PDFs and comics have pages, not text, so alignment, read-along, and the narrated EPUB are unavailable.</span>
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
  if (!hasEpub(work) && work.files.some((f) => f.kind === "ebook")) return <NeedsEpub />;
  return <ReadAloudRows work={work} active={active} />;
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
  useEffect(() => {
    if (!active || (st?.status !== "generating" && !aligning)) return;
    const id = window.setTimeout(refresh, st?.status === "generating" ? POLL_MS : ALIGN_POLL_MS);
    return () => window.clearTimeout(id);
  }, [active, st, aligning, refresh]);

  const retry = async () => {
    try {
      await retryAlignment(work.id);
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
    const canRetry = Boolean(showAlign && align.retry && canUpload);
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
        {canRetry ? (
          <button className="btn sm ghost" onClick={retry} aria-label={`Retry aligning ${work.title}`}>Retry</button>
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
