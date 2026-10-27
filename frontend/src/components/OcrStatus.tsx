import { useCallback, useEffect, useState } from "react";
import { ApiError, del, get, post } from "../lib/api";
import { bytes } from "../lib/format";
import { useStore } from "../lib/store";
import type { BookFile } from "../lib/types";
import { Download } from "./Icons";

/** GET /api/files/{id}/ocr (docs/ocr.md). */
export interface OcrInfo {
  file_id: number;
  status: "checking" | "not_needed" | "unreadable" | "needs_ocr" | "unavailable" | "queued" | "running" | "failed" | "done";
  pages: number | null;
  language: string | null;
  default_language: string;
  languages: string[];
  engine: string | null;
  worker: boolean;
  progress: number | null;
  message: string | null;
  error: string | null;
  result: { words: number; pages: number; pages_failed: number; language: string } | null;
  download_url: string | null;
  size: number | null;
}

const POLL_MS = 4000;
const LANG_NAMES: Record<string, string> = {
  eng: "English", fra: "French", deu: "German", spa: "Spanish", ita: "Italian", por: "Portuguese",
  nld: "Dutch", rus: "Russian", pol: "Polish", swe: "Swedish", jpn: "Japanese", chi_sim: "Chinese (simplified)",
  chi_tra: "Chinese (traditional)", ara: "Arabic", heb: "Hebrew", hin: "Hindi", kor: "Korean", lat: "Latin",
};
const langName = (code: string) => code.split("+").map((c) => LANG_NAMES[c] ?? c).join(" + ");

/**
 * Text recognition for one PDF: shown only for scans (a text PDF says nothing). Queued,
 * running with progress, failed with Retry, done with the searchable copy.
 */
export function OcrStatus({ file, title }: { file: BookFile; title: string }) {
  const { canUpload } = useStore();
  const [info, setInfo] = useState<OcrInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lang, setLang] = useState<string>("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setInfo(await get<OcrInfo>(`/api/files/${file.id}/ocr`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [file.id]);

  useEffect(() => { refresh(); }, [refresh]);
  const live = info?.status === "queued" || info?.status === "running" || info?.status === "checking";
  useEffect(() => {
    if (!live) return;
    const id = window.setTimeout(refresh, POLL_MS);
    return () => window.clearTimeout(id);
  }, [live, info, refresh]);

  const act = async (fn: () => Promise<OcrInfo>) => {
    setBusy(true);
    try {
      setInfo(await fn());
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  };
  const installed = info?.languages.length ? info.languages : [info?.default_language ?? "eng"];
  // what the picker shows: the user's pick, else the last run's language when it is installed
  const chosen = lang || (info?.language && installed.includes(info.language) ? info.language : installed.includes(info?.default_language ?? "") ? info!.default_language : installed[0]);
  const start = () => act(() => post<OcrInfo>(`/api/files/${file.id}/ocr`, { language: chosen }));
  const remove = () => act(() => del<OcrInfo>(`/api/files/${file.id}/ocr`));

  if (!info || info.status === "not_needed" || info.status === "checking") return null;

  const picker = canUpload && installed.length > 1 && (
    <select className="ocr-lang" aria-label="Language of the text" value={chosen} onChange={(e) => setLang(e.target.value)}>
      {installed.map((l) => <option key={l} value={l}>{langName(l)}</option>)}
    </select>
  );
  const pct = info.progress != null ? Math.floor(info.progress * 100) : null;

  return (
    <div className="ocr" data-state={info.status} aria-live="polite">
      {info.status === "needs_ocr" && (
        <>
          <span className="hint"><strong>Scanned PDF</strong> · pictures of pages, no text to search yet</span>
          {canUpload && <span className="row ocr-actions">{picker}<button className="btn sm" disabled={busy} onClick={start} aria-label={`Recognise the text of ${title}`}>Recognise text</button></span>}
        </>
      )}
      {info.status === "unavailable" && <span className="hint"><strong>Scanned PDF</strong> · {info.message}</span>}
      {info.status === "unreadable" && <span className="hint"><strong>Scanned PDF</strong> · this PDF can’t be read for text recognition ({info.error})</span>}
      {info.status === "queued" && (
        <span className="hint"><span className="spinner sm" role="img" aria-label="Queued" /> Text recognition queued{info.message ? ` · ${info.message}` : ""}{info.error ? ` · retrying after: ${info.error}` : ""}</span>
      )}
      {info.status === "running" && (
        <>
          <span className="hint">Recognising text in {langName(info.language ?? info.default_language)}{info.message ? ` · ${info.message}` : ""}</span>
          <span className="progress-bar" role="progressbar" aria-label="Text recognition progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct ?? 0}>
            <span style={{ width: `${pct ?? 0}%` }} />
          </span>
        </>
      )}
      {info.status === "failed" && (
        <>
          <span className="hint error-text"><strong>Text recognition failed</strong> · {info.error}</span>
          {canUpload && <span className="row ocr-actions">{picker}<button className="btn sm" disabled={busy} onClick={start} aria-label={`Retry recognising the text of ${title}`}>Retry</button></span>}
        </>
      )}
      {info.status === "done" && (
        <>
          <span className="hint">
            <strong>Searchable copy</strong> · text recognised{info.result ? ` in ${langName(info.result.language)}, ${info.result.words.toLocaleString()}\u00a0words` : ""}{info.size != null ? ` · ${bytes(info.size)}` : ""}
            {info.result && info.result.pages_failed > 0 ? ` · ${info.result.pages_failed} page${info.result.pages_failed === 1 ? "" : "s"} could not be read` : ""}
          </span>
          <span className="row ocr-actions">
            {info.download_url && <a className="btn sm ghost" href={info.download_url} aria-label={`Download the searchable PDF of ${title}`}><Download /> Searchable PDF</a>}
            {canUpload && picker}
            {canUpload && info.result && chosen !== info.result.language && <button className="btn sm ghost" disabled={busy} onClick={start}>Recognise again</button>}
            {canUpload && <button className="btn sm ghost" disabled={busy} onClick={remove} aria-label={`Delete the searchable copy of ${title}`}>Delete copy</button>}
          </span>
        </>
      )}
      {error && <span className="hint error-text">{error}</span>}
    </div>
  );
}
