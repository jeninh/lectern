import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { MarkList } from "../components/AudioBookmarks";
import { EmptyState } from "../components/EmptyState";
import { Close, Download, Highlighter, Quote as QuoteIcon, Search as SearchIcon } from "../components/Icons";
import { QuoteFeed } from "../components/QuoteFeed";
import { PageTitle } from "../components/Shell";
import { ApiError, get } from "../lib/api";
import type { AudioBookmark, AudioHit, SearchWithAudio, SummaryWithAudio } from "../lib/audiobookmarks";
import { annotationWhere, type HighlightFilters, highlightHitPath, highlightsQuery } from "../lib/annotations";
import { useStore } from "../lib/store";
import type { HighlightHit, Work } from "../lib/types";

const LIMIT = 200;
const DEBOUNCE_MS = 250;
const KEYS = ["q", "work", "author", "tag", "since", "until"] as const;

function Marked({ parts, text }: { parts: [string, boolean][] | null; text: string }) {
  if (!parts) return <>{text}</>;
  return <>{parts.map(([t, m], k) => (m ? <mark key={k}>{t}</mark> : <span key={k}>{t}</span>))}</>;
}

/** All your KOReader highlights and notes, and your audiobook bookmarks and clips, searched and filtered by book, author, tag, and date,
 * with the Markdown export of what is shown (docs/koreader-plugin.md#searching-your-highlights).
 * The search and filters live in the URL, so a filtered view can be bookmarked. `view=quotes`
 * shows them as the quotes feed instead (components/QuoteFeed.tsx, issue #83). */
export function HighlightsPage() {
  const { works } = useStore();
  const [params, setParams] = useSearchParams();
  const filters = useMemo(() => Object.fromEntries(KEYS.map((k) => [k, params.get(k) ?? ""])) as Required<HighlightFilters>, [params]);
  const [typed, setTyped] = useState(filters.q);
  const [summary, setSummary] = useState<SummaryWithAudio | null>(null);
  const [data, setData] = useState<SearchWithAudio | null>(null);
  const [error, setError] = useState<string | null>(null);
  const query = highlightsQuery(filters);
  const quotes = params.get("view") === "quotes";
  // Highlights and audio marks together (issue #81).
  const all = (summary?.total ?? 0) + (summary?.audio_total ?? 0);
  const audioHits = data?.audio ?? [];
  const shown = (data?.total ?? 0) + (data?.audio_total ?? 0);

  useEffect(() => {
    get<SummaryWithAudio>("/api/annotations").then(setSummary).catch((e) => setError((e as Error).message));
  }, []);

  const set = (next: HighlightFilters) => setParams(highlightsQuery({ ...filters, ...next }), { replace: true });

  // The search box updates the URL after a pause in typing.
  useEffect(() => {
    if (typed.trim() === filters.q.trim()) return;
    const t = window.setTimeout(() => set({ q: typed }), DEBOUNCE_MS);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [typed]);

  useEffect(() => {
    if (!all || quotes) return;
    let live = true;
    get<SearchWithAudio>(`/api/annotations/search?${query}${query ? "&" : ""}limit=${LIMIT}&partial=false`)
      .then((d) => {
        if (!live) return;
        setData(d);
        setError(null);
      })
      .catch((e) => live && setError(e instanceof ApiError ? e.message : (e as Error).message));
    return () => {
      live = false;
    };
  }, [query, all, quotes]);

  const books = summary?.works ?? [];
  const authors = useMemo(() => [...new Set(books.map((w) => w.author).filter(Boolean))].sort((a, b) => a.localeCompare(b)), [books]);
  const tags = useMemo(() => [...new Set(books.flatMap((w) => w.tags ?? []))].sort((a, b) => a.localeCompare(b)), [books]);
  const filtered = Boolean(query);
  const exportUrl = `/api/annotations.md${query ? `?${query}` : ""}`;
  const clear = () => {
    setTyped("");
    setParams(new URLSearchParams(), { replace: true });
  };

  return (
    <>
      <PageTitle sub="Notes and highlights from KOReader">Highlights</PageTitle>
      {summary && summary.total > 0 && (
        <div className="seg seg-text highlights-view" role="group" aria-label="Highlights view">
          <button aria-pressed={!quotes} onClick={() => setParams(new URLSearchParams(), { replace: true })}><SearchIcon /> Search</button>
          <button aria-pressed={quotes} onClick={() => setParams("view=quotes", { replace: true })}><QuoteIcon /> Quotes</button>
        </div>
      )}
      {summary && summary.total > 0 && quotes && <QuoteFeed books={summary.works} />}
      {summary && all === 0 && (
        <EmptyState icon={<Highlighter />} title="No highlights yet" action={<Link className="btn" to="/app/devices">Set up KOReader</Link>}>
          Passages you highlight in KOReader, and the notes you add to them, show up here and on each book once the Lectern plugin syncs. So do the bookmarks and clips you make while listening to an audiobook.
        </EmptyState>
      )}
      {summary && all > 0 && !quotes && (
        <div className="stack">
          <div className="filter-bar highlights-filters" role="search" aria-label="Highlights">
            <div className="filter-input">
              <SearchIcon aria-hidden="true" />
              <label className="sr-only" htmlFor="hl-q">Search your highlights and notes</label>
              <input id="hl-q" data-page-filter className="input" type="search" placeholder="Search highlights" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" spellCheck={false} />
              {typed && (
                <button className="clear" aria-label="Clear the search" onClick={() => setTyped("")}>
                  <Close />
                </button>
              )}
            </div>
            <label className="sr-only" htmlFor="hl-work">Book</label>
            <select id="hl-work" className="select sm" value={filters.work} onChange={(e) => set({ work: e.target.value })}>
              <option value="">All books</option>
              {books.map((w) => (
                <option key={w.work_id} value={String(w.work_id)}>{w.title}</option>
              ))}
            </select>
            <label className="sr-only" htmlFor="hl-author">Author</label>
            <select id="hl-author" className="select sm" value={filters.author} onChange={(e) => set({ author: e.target.value })}>
              <option value="">All authors</option>
              {authors.map((a) => (
                <option key={a} value={a}>{a}</option>
              ))}
            </select>
            {tags.length > 0 && (
              <>
                <label className="sr-only" htmlFor="hl-tag">Tag</label>
                <select id="hl-tag" className="select sm" value={filters.tag} onChange={(e) => set({ tag: e.target.value })}>
                  <option value="">All tags</option>
                  {tags.map((t) => (
                    <option key={t} value={t}>{t}</option>
                  ))}
                </select>
              </>
            )}
            <div className="row highlights-date" role="group" aria-label="Date">
              <label className="row highlights-date">
                <span className="hint">From</span>
                <input className="input sm" type="date" value={filters.since} max={filters.until || undefined} onChange={(e) => set({ since: e.target.value })} />
              </label>
              <label className="row highlights-date">
                <span className="hint">to</span>
                <input className="input sm" type="date" value={filters.until} min={filters.since || undefined} onChange={(e) => set({ until: e.target.value })} />
              </label>
            </div>
          </div>
          <p className="hint highlights-help">
            Words match in any order, accents and case aside. Start with <code>note:</code> or <code>highlight:</code> to search only notes or only the highlighted text.
          </p>

          {error && <p className="hint" role="alert">{error}</p>}
          {data && (
            <div className="row highlights-head">
              <span className="hint" role="status">
                {filtered
                  ? `${shown} of ${all} ${summary.audio_total ? "highlights and audio bookmarks" : all === 1 ? "highlight" : "highlights"}`
                  : `${summary.total} ${summary.total === 1 ? "highlight" : "highlights"}${summary.audio_total ? ` and ${summary.audio_total} audio ${summary.audio_total === 1 ? "bookmark" : "bookmarks"}` : ""} in ${books.length} ${books.length === 1 ? "book" : "books"}`}
                {data.truncated && ` · showing the first ${data.results.length}`}
              </span>
              <span className="row">
                {filtered && <button className="btn sm ghost" onClick={clear}>Clear filters</button>}
                {shown > 0 && (
                  <a className="btn sm" href={exportUrl} download>
                    <Download /> {filtered ? "Export these as Markdown" : "Export all as Markdown"}
                  </a>
                )}
              </span>
            </div>
          )}
          {data && shown === 0 && (
            <EmptyState icon={<SearchIcon />} title={filters.q ? `No highlights match “${filters.q}”` : "No highlights match these filters"} action={<button className="btn" onClick={clear}>Clear filters</button>}>
              Try fewer words, another book, or a wider date range.
            </EmptyState>
          )}
          {data && data.results.length > 0 && (
            <ul className="highlight-results">
              {data.results.map((h) => (
                <HighlightItem key={h.annotation.id} hit={h} to={highlightHitPath(h, works.find((w) => w.id === h.work_id))} />
              ))}
            </ul>
          )}
          {audioHits.length > 0 && (
            <section aria-labelledby="hl-audio-h" className="audio-marks">
              <h2 id="hl-audio-h" className="detail-h">Audio bookmarks</h2>
              {groupByWork(audioHits).map(([h, list]) => {
                const work = works.find((w) => w.id === h.work_id);
                return (
                  <div key={h.work_id} className="highlight-group">
                    <div className="highlight-book">
                      <Link to={`/app/library/${h.work_id}`}><strong>{h.title}</strong></Link>
                      {h.author && <span className="hint"> · {h.author}</span>}
                    </div>
                    <MarkList work={work ?? ({ id: h.work_id, title: h.title, author: h.author, files: [] } as unknown as Work)} marks={list} />
                  </div>
                );
              })}
            </section>
          )}
        </div>
      )}
    </>
  );
}

function HighlightItem({ hit, to }: { hit: HighlightHit; to: string }) {
  const a = hit.annotation;
  return (
    <li className="highlight" data-color={a.color ?? undefined}>
      <div className="highlight-book">
        <Link to={`/app/library/${hit.work_id}`}><strong>{hit.title}</strong></Link>
        {hit.author && <span className="hint"> · {hit.author}</span>}
        {a.chapter && <span className="hint"> · {a.chapter}</span>}
      </div>
      <blockquote><Marked parts={hit.text_parts} text={a.text} /></blockquote>
      {a.note && (
        <p className="highlight-note">
          <strong>Note:</strong> <Marked parts={hit.note_parts} text={a.note} />
        </p>
      )}
      <div className="hint highlight-meta">
        <span>{annotationWhere(a)}</span>
        <Link className="btn sm ghost" to={to}>{to.startsWith("/app/reader/") ? "Open in reader" : "Open book"}</Link>
      </div>
    </li>
  );
}

/** Audio hits under their book, in the order the server sent them. */
function groupByWork(hits: AudioHit[]): [AudioHit, AudioBookmark[]][] {
  const out = new Map<number, [AudioHit, AudioBookmark[]]>();
  for (const h of hits) {
    const g = out.get(h.work_id) ?? [h, []];
    g[1].push(h.bookmark);
    out.set(h.work_id, g);
  }
  return [...out.values()];
}
