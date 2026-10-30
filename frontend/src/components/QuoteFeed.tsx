import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ApiError, get, post } from "../lib/api";
import { pathLabel } from "../lib/shelves";
import { localDay, type Quote, type QuoteAnnotation, type QuoteFeed as Feed, type QuoteFilters, type QuoteOfTheDay, quotePath, quotesQuery } from "../lib/quotes";
import { useStore } from "../lib/store";
import type { AnnotationsSummary } from "../lib/types";
import { Cover, tintStyle } from "./Cover";
import { EmptyState } from "./EmptyState";
import { Heart, Highlighter, Shuffle } from "./Icons";

const PAGE = 30;
const KEYS = ["work", "author", "shelf", "tag", "order", "seed", "show"] as const;
const newSeed = () => Math.random().toString(36).slice(2, 10);

/** The Highlights page's Quotes view (issue #83): your highlights as cards, newest first or
 * shuffled, filtered by book, author, shelf, and tag; favourite or hide each one. The filters live
 * in the URL beside `view=quotes`, so a view can be bookmarked. */
export function QuoteFeed({ books }: { books: AnnotationsSummary["works"] }) {
  const { works, shelves } = useStore();
  const [params, setParams] = useSearchParams();
  const filters = useMemo(() => Object.fromEntries(KEYS.map((k) => [k, params.get(k) ?? ""])) as Required<QuoteFilters>, [params]);
  const query = quotesQuery(filters);
  const [data, setData] = useState<Feed | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const set = (next: QuoteFilters) => {
    const q = quotesQuery({ ...filters, ...next });
    setParams(`view=quotes${q ? `&${q}` : ""}`, { replace: true });
  };

  useEffect(() => {
    let live = true;
    get<Feed>(`/api/quotes?${query}${query ? "&" : ""}limit=${PAGE}`)
      .then((d) => {
        if (!live) return;
        setData(d);
        setError(null);
      })
      .catch((e) => live && setError(e instanceof ApiError ? e.message : (e as Error).message));
    return () => {
      live = false;
    };
  }, [query]);

  const more = async () => {
    if (!data) return;
    setBusy(true);
    try {
      const d = await get<Feed>(`/api/quotes?${query}${query ? "&" : ""}limit=${PAGE}&offset=${data.results.length}`);
      setData({ ...d, results: [...data.results, ...d.results] });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const mark = async (q: Quote, change: Partial<Pick<QuoteAnnotation, "favourite" | "hidden">>) => {
    try {
      const next = await post<Quote>(`/api/annotations/${q.annotation.id}/mark`, change);
      const a = next.annotation;
      const stays = filters.show === "hidden" ? a.hidden : !a.hidden && (filters.show !== "favourites" || a.favourite);
      setData((d) =>
        d && {
          ...d,
          total: stays ? d.total : d.total - 1,
          results: stays ? d.results.map((r) => (r.annotation.id === a.id ? next : r)) : d.results.filter((r) => r.annotation.id !== a.id),
        },
      );
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const setDaily = async (daily: boolean) => {
    try {
      const r = await post<{ daily: boolean }>("/api/quotes/settings", { daily });
      setData((d) => d && { ...d, daily: r.daily });
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const authors = useMemo(() => [...new Set(books.map((w) => w.author).filter(Boolean))].sort((a, b) => a.localeCompare(b)), [books]);
  const tags = useMemo(() => [...new Set(books.flatMap((w) => w.tags ?? []))].sort((a, b) => a.localeCompare(b)), [books]);
  const shuffled = filters.order === "shuffle";
  const show = filters.show || "all";
  const narrowed = Boolean(filters.work || filters.author || filters.shelf || filters.tag || show !== "all");

  return (
    <div className="stack">
      <div className="filter-bar quotes-filters" role="search" aria-label="Quotes">
        <label className="sr-only" htmlFor="qt-work">Book</label>
        <select id="qt-work" className="select sm" value={filters.work} onChange={(e) => set({ work: e.target.value })}>
          <option value="">All books</option>
          {books.map((w) => (
            <option key={w.work_id} value={String(w.work_id)}>{w.title}</option>
          ))}
        </select>
        <label className="sr-only" htmlFor="qt-author">Author</label>
        <select id="qt-author" className="select sm" value={filters.author} onChange={(e) => set({ author: e.target.value })}>
          <option value="">All authors</option>
          {authors.map((a) => (
            <option key={a} value={a}>{a}</option>
          ))}
        </select>
        {shelves.length > 0 && (
          <>
            <label className="sr-only" htmlFor="qt-shelf">Shelf</label>
            <select id="qt-shelf" className="select sm" value={filters.shelf} onChange={(e) => set({ shelf: e.target.value })}>
              <option value="">All shelves</option>
              {shelves.map((s) => (
                <option key={s.id} value={String(s.id)}>{pathLabel(s)}</option>
              ))}
            </select>
          </>
        )}
        {tags.length > 0 && (
          <>
            <label className="sr-only" htmlFor="qt-tag">Tag</label>
            <select id="qt-tag" className="select sm" value={filters.tag} onChange={(e) => set({ tag: e.target.value })}>
              <option value="">All tags</option>
              {tags.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </>
        )}
        <label className="sr-only" htmlFor="qt-show">Show</label>
        <select id="qt-show" className="select sm" value={show} onChange={(e) => set({ show: e.target.value as QuoteFilters["show"] })}>
          <option value="all">All quotes</option>
          <option value="favourites">Favourites</option>
          <option value="hidden">Hidden</option>
        </select>
        <div className="seg seg-text sm" role="group" aria-label="Order">
          <button aria-pressed={!shuffled} onClick={() => set({ order: "newest" })}>Newest</button>
          <button aria-pressed={shuffled} onClick={() => set({ order: "shuffle", seed: newSeed() })}><Shuffle /> {shuffled ? "Shuffle again" : "Shuffle"}</button>
        </div>
      </div>

      {error && <p className="hint" role="alert">{error}</p>}
      {data && (
        <div className="row highlights-head">
          <span className="hint" role="status">{`${data.total} ${data.total === 1 ? "quote" : "quotes"}`}</span>
          <label className="row quotes-daily">
            <input type="checkbox" checked={data.daily} onChange={(e) => void setDaily(e.target.checked)} />
            <span>Highlight of the day on Home</span>
          </label>
        </div>
      )}
      {data && data.total === 0 && (
        <EmptyState icon={<Highlighter />} title={show === "hidden" ? "Nothing hidden" : show === "favourites" ? "No favourites yet" : "No quotes match these filters"} action={narrowed ? <button className="btn" onClick={() => setParams("view=quotes", { replace: true })}>Show all quotes</button> : undefined}>
          {show === "hidden" ? "Quotes you hide leave the feed and the highlight of the day, and wait here." : show === "favourites" ? "Tap the heart on a quote to keep it here." : "Try another book, author, shelf, or tag."}
        </EmptyState>
      )}
      {data && data.results.length > 0 && (
        <ul className="quote-feed">
          {data.results.map((q) => {
            const work = works.find((w) => w.id === q.work_id);
            return <QuoteCard key={q.annotation.id} quote={q} work={work} to={quotePath(q, work)} onMark={(c) => void mark(q, c)} />;
          })}
        </ul>
      )}
      {data && data.results.length < data.total && (
        <button className="btn ghost quotes-more" disabled={busy} onClick={() => void more()}>Show more</button>
      )}
    </div>
  );
}

type CoverWork = Parameters<typeof Cover>[0]["work"];

function QuoteCard({ quote, work, to, onMark }: { quote: Quote; work: CoverWork | undefined; to: string; onMark: (c: Partial<Pick<QuoteAnnotation, "favourite" | "hidden">>) => void }) {
  const a = quote.annotation;
  return (
    <li className="quote-card highlight" data-color={a.color ?? undefined}>
      {work && (
        <Link className="quote-cover" to={`/app/library/${quote.work_id}`} tabIndex={-1} aria-hidden="true" style={tintStyle(work)}>
          <Cover work={work} />
        </Link>
      )}
      <div className="quote-body">
        <blockquote>{a.text}</blockquote>
        {a.note && (
          <p className="highlight-note">
            <strong>Note:</strong> {a.note}
          </p>
        )}
        <div className="quote-foot">
          <span className="quote-source">
            <Link to={`/app/library/${quote.work_id}`}><strong>{quote.title}</strong></Link>
            {quote.author && <span className="hint"> · {quote.author}</span>}
          </span>
          <span className="row quote-actions">
            <button className="btn sm ghost" aria-pressed={a.favourite} aria-label={a.favourite ? "Remove from favourites" : "Add to favourites"} title={a.favourite ? "Remove from favourites" : "Add to favourites"} onClick={() => onMark({ favourite: !a.favourite })}>
              <Heart />
            </button>
            <button className="btn sm ghost" onClick={() => onMark({ hidden: !a.hidden })}>{a.hidden ? "Unhide" : "Hide"}</button>
            <Link className="btn sm" to={to}>{to.startsWith("/app/reader/") ? "Open in reader" : "Open book"}</Link>
          </span>
        </div>
      </div>
    </li>
  );
}

/** Home's highlight of the day: one of your own highlights, the same all day (your local date),
 * another tomorrow. Nothing shows when it is turned off or you have none. */
export function QuoteOfTheDayCard() {
  const { works, now } = useStore();
  const day = localDay(new Date(now));
  const [data, setData] = useState<QuoteOfTheDay | null>(null);

  useEffect(() => {
    let live = true;
    get<QuoteOfTheDay>(`/api/quotes/today?day=${day}`)
      .then((d) => live && setData(d))
      .catch(() => live && setData(null)); // offline or refused: no card
    return () => {
      live = false;
    };
  }, [day]);

  const q = data?.quote;
  if (!q) return null;
  const work = works.find((w) => w.id === q.work_id);
  const to = quotePath(q, work);
  const turnOff = async () => {
    await post("/api/quotes/settings", { daily: false });
    setData({ ...data!, enabled: false, quote: null });
  };

  return (
    <section className="card quote-today highlight" aria-labelledby="quote-h" data-color={q.annotation.color ?? undefined}>
      <div className="section-head">
        <h2 id="quote-h">Highlight of the day</h2>
        <Link className="more" to="/app/highlights?view=quotes">All quotes →</Link>
      </div>
      <blockquote>{q.annotation.text}</blockquote>
      {q.annotation.note && (
        <p className="highlight-note">
          <strong>Note:</strong> {q.annotation.note}
        </p>
      )}
      <div className="quote-foot">
        <span className="quote-source">
          <strong>{q.title}</strong>
          {q.author && <span className="hint"> · {q.author}</span>}
        </span>
        <span className="row quote-actions">
          <button className="btn sm ghost" title="Turn it back on from Highlights → Quotes" onClick={() => void turnOff().catch(() => {})}>Turn off</button>
          <Link className="btn sm" to={to}>{to.startsWith("/app/reader/") ? "Open in reader" : "Open book"}</Link>
        </span>
      </div>
    </section>
  );
}
