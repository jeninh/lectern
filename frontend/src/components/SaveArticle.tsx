import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { relativeTime } from "../lib/format";
import { useVisiblePolling } from "../lib/hooks";
import { articleActive, articleError, articlesState, isSavableUrl, saveArticle, type ArticlesState } from "../lib/ingest";
import { useStore } from "../lib/store";
import { Sheet } from "./Sheet";

/** "Save a web article": paste a link (or arrive from the share sheet with one) and the
 * server turns the article into an EPUB book (docs/save-articles.md). */
export function SaveArticle({ open, initialUrl, onClose }: { open: boolean; initialUrl?: string; onClose: () => void }) {
  return (
    <Sheet open={open} onClose={onClose} title="Save a web article" variant="dialog" labelledBy="article-title">
      {open && <SaveForm initialUrl={initialUrl ?? ""} onClose={onClose} />}
    </Sheet>
  );
}

function SaveForm({ initialUrl, onClose }: { initialUrl: string; onClose: () => void }) {
  const { refreshLibrary, now } = useStore();
  const [url, setUrl] = useState(initialUrl);
  const [state, setState] = useState<ArticlesState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => articlesState().then(setState).catch((e) => setError((e as Error).message)), []);
  useEffect(() => {
    load();
  }, [load]);
  const active = !!state?.jobs.some(articleActive);
  useVisiblePolling(() => {
    if (active) load();
  }, 2000);
  // A save that just finished added a book: show it in the library.
  const wasActive = useRef(false);
  useEffect(() => {
    if (wasActive.current && !active) refreshLibrary().catch(() => {});
    wasActive.current = active;
  }, [active, refreshLibrary]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!isSavableUrl(url)) {
      setError(articleError("bad_url"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await saveArticle(url.trim());
      setUrl("");
      await load();
    } catch (ex) {
      setError((ex as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const off = state !== null && !state.enabled;
  return (
    <form onSubmit={submit} className="stack" style={{ gap: 14 }}>
      <p className="sub" style={{ margin: 0 }}>
        Lectern fetches the page, keeps the article and its pictures, and adds it to your library as an EPUB, ready for your e-reader. <Link to="/app/docs/save-articles" onClick={onClose}>How it works</Link>
      </p>
      {off && (
        <div className="alert warn" role="status">
          {articleError(state.reason)}
        </div>
      )}
      <label className="field">
        <span>Link to the article</span>
        <input
          className="input"
          type="url"
          inputMode="url"
          placeholder="https://"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          maxLength={2048}
          required
          autoComplete="off"
          disabled={off}
          data-autofocus
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? "article-error" : undefined}
        />
      </label>
      {error && (
        <div className="field-error" id="article-error" role="alert">
          {error}
        </div>
      )}
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button type="button" className="btn ghost" onClick={onClose}>Close</button>
        <button type="submit" className="btn primary" disabled={busy || off || !url.trim()}>{busy ? "Saving…" : "Save"}</button>
      </div>
      {state && state.jobs.length > 0 && (
        <section aria-labelledby="article-recent">
          <h3 className="detail-h" id="article-recent">Recent saves</h3>
          <ul className="article-list" aria-live="polite">
            {state.jobs.slice(0, 8).map((j) => (
              <li key={j.id}>
                <div className="article-what">
                  {j.status === "succeeded" && j.work_id ? (
                    <Link to={`/app/library/${j.work_id}`} onClick={onClose}>{j.title ?? j.url}</Link>
                  ) : (
                    <span>{j.title ?? j.url}</span>
                  )}
                  <span className="hint">{j.created_at ? relativeTime(j.created_at, now) : ""}</span>
                </div>
                <div className="hint">
                  {articleActive(j) && <span className="badge accent">saving…</span>}
                  {j.status === "succeeded" && (
                    <>
                      <span className="badge ok">saved</span>
                      {j.skipped_images ? ` ${j.skipped_images} picture${j.skipped_images === 1 ? "" : "s"} left out` : ""}
                    </>
                  )}
                  {j.status === "failed" && (
                    <>
                      <span className="badge warn">not saved</span> {articleError(j.error)}
                      {j.error === "already_saved" && j.work_id && (
                        <>
                          {" "}
                          <Link to={`/app/library/${j.work_id}`} onClick={onClose}>Open it</Link>
                        </>
                      )}
                    </>
                  )}
                  {j.status === "cancelled" && <span className="badge">cancelled</span>}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}
    </form>
  );
}
