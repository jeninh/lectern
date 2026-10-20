import { useEffect, useMemo, useRef } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Cover, tintStyle } from "../components/Cover";
import { EmptyState } from "../components/EmptyState";
import { Book, Close, Download, Search as SearchIcon, Upload } from "../components/Icons";
import { fold } from "../lib/fuzzy";
import { Sheet } from "../components/Sheet";
import { PageTitle } from "../components/Shell";
import { absoluteTime, bytes, percent, relativeTime } from "../lib/format";
import { useLocalStorage } from "../lib/hooks";
import { useStore } from "../lib/store";
import type { Work } from "../lib/types";

type Sort = "title" | "author" | "added";
type Status = "all" | "reading" | "unread" | "finished";
const FINISHED = 0.985;

function statusOf(w: Work): Exclude<Status, "all"> {
  const p = w.progress?.percentage ?? 0;
  if (!w.progress || p <= 0) return "unread";
  return p >= FINISHED ? "finished" : "reading";
}

function Highlight({ text, q }: { text: string; q: string }) {
  if (!q) return <>{text}</>;
  const i = fold(text).indexOf(fold(q));
  if (i < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, i)}
      <mark>{text.slice(i, i + q.length)}</mark>
      {text.slice(i + q.length)}
    </>
  );
}

/** Small circular progress indicator layered on the cover corner. */
export function ProgressRing({ value, size = 34 }: { value: number; size?: number }) {
  const r = (size - 5) / 2;
  const c = 2 * Math.PI * r;
  const pct = Math.round(value * 100);
  return (
    <span className="ring" role="img" aria-label={`${pct}% read`} style={{ width: size, height: size }}>
      <svg viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <circle cx={size / 2} cy={size / 2} r={r} className="track" />
        <circle cx={size / 2} cy={size / 2} r={r} className="value" strokeDasharray={c} strokeDashoffset={c * (1 - value)} />
      </svg>
      <span>{value >= FINISHED ? "✓" : pct}</span>
    </span>
  );
}

const STATUS_LABEL: Record<Status, string> = { all: "All", reading: "Reading", unread: "Not started", finished: "Finished" };
const SORT_LABEL: Record<Sort, string> = { title: "Title", author: "Author", added: "Newest" };

export function LibraryPage() {
  const { works, worksLoaded, me, refreshLibrary, canUpload, pickBooks } = useStore();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const sort = (params.get("sort") as Sort) || "title";
  const status = (params.get("status") as Status) || "all";
  const [view, setView] = useLocalStorage<"grid" | "list">("lectern.libraryView", "grid");
  const { id } = useParams();
  const nav = useNavigate();
  const loc = useLocation();
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    refreshLibrary().catch(() => {});
  }, [refreshLibrary]);

  const counts = useMemo(() => {
    const c: Record<Status, number> = { all: works.length, reading: 0, unread: 0, finished: 0 };
    for (const w of works) c[statusOf(w)] += 1;
    return c;
  }, [works]);

  const filtered = useMemo(() => {
    const needle = fold(q.trim());
    let list = needle ? works.filter((w) => fold(`${w.title} ${w.author} ${w.isbn ?? ""}`).includes(needle)) : works.slice();
    if (status !== "all") list = list.filter((w) => statusOf(w) === status);
    const by: Record<Sort, (a: Work, b: Work) => number> = {
      title: (a, b) => a.sort_title.localeCompare(b.sort_title),
      author: (a, b) => a.sort_author.localeCompare(b.sort_author) || a.sort_title.localeCompare(b.sort_title),
      added: (a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""),
    };
    return list.sort(by[sort]);
  }, [works, q, sort, status]);

  const selected = id ? works.find((w) => w.id === Number(id)) ?? null : null;
  const setParam = (k: string, v: string, dflt: string) => {
    const next = new URLSearchParams(params);
    if (v && v !== dflt) next.set(k, v);
    else next.delete(k);
    setParams(next, { replace: true });
  };
  const open = (w: Work) => nav(`/app/library/${w.id}${loc.search}`);
  const filtering = !!q || status !== "all";

  return (
    <>
      <PageTitle
        sub={worksLoaded ? `${works.length} ${works.length === 1 ? "book" : "books"} · ${counts.reading} reading · ${counts.finished} finished` : undefined}
        aside={canUpload ? <button className="btn ghost" onClick={pickBooks}><Upload /> Upload</button> : undefined}
      >
        Library
      </PageTitle>
      <div className="filter-bar" role="search" aria-label="Library">
        <div className="filter-input">
          <SearchIcon aria-hidden="true" />
          <label className="sr-only" htmlFor="lib-q">Filter by title, author, or ISBN</label>
          <input
            id="lib-q"
            ref={input}
            data-page-filter
            className="input"
            placeholder="Filter by title or author"
            value={q}
            onChange={(e) => setParam("q", e.target.value, "")}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.stopPropagation();
                if (q) setParam("q", "", "");
                else input.current?.blur();
              } else if (e.key === "Enter" && filtered[0]) {
                // Without this the Enter keypress lands on the sheet's freshly focused Close button.
                e.preventDefault();
                open(filtered[0]);
              }
            }}
            autoComplete="off"
            spellCheck={false}
          />
          {q ? (
            <button className="clear" onClick={() => (setParam("q", "", ""), input.current?.focus())} aria-label="Clear filter"><Close /></button>
          ) : (
            <kbd aria-hidden="true">/</kbd>
          )}
        </div>
        <div className="chips" role="group" aria-label="Reading status">
          {(Object.keys(STATUS_LABEL) as Status[]).map((s) => (
            <button key={s} className="chip" aria-pressed={status === s} onClick={() => setParam("status", s, "all")}>
              {STATUS_LABEL[s]} <span className="n">{counts[s]}</span>
            </button>
          ))}
        </div>
        <div className="chips" role="group" aria-label="Sort by">
          {(Object.keys(SORT_LABEL) as Sort[]).map((s) => (
            <button key={s} className="chip subtle" aria-pressed={sort === s} onClick={() => setParam("sort", s, "title")}>
              {SORT_LABEL[s]}
            </button>
          ))}
        </div>
        <div className="seg" role="group" aria-label="Layout">
          <button aria-pressed={view === "grid"} onClick={() => setView("grid")} aria-label="Grid" title="Grid"><GridIcon /></button>
          <button aria-pressed={view === "list"} onClick={() => setView("list")} aria-label="List" title="List"><ListIcon /></button>
        </div>
        <span className="sr-only" aria-live="polite">{filtering ? `${filtered.length} of ${works.length} books shown` : ""}</span>
      </div>

      {!worksLoaded ? (
        <div className="book-grid" aria-busy="true" aria-label="Loading">{Array.from({ length: 6 }, (_, i) => <div key={i} className="book skeleton"><div className="cover" /></div>)}</div>
      ) : works.length === 0 ? (
        <EmptyState icon={<Book />} title="No books yet" action={canUpload ? <button className="btn primary" onClick={pickBooks}><Upload /> Upload EPUBs</button> : undefined}>
          Upload EPUBs or drop them into <code>{me?.inbox_path ?? "the inbox"}</code>. Lectern reads their metadata, files them under author and title, and lists them here.
        </EmptyState>
      ) : filtered.length === 0 ? (
        <EmptyState
          icon={<SearchIcon />}
          title={q ? `Nothing matches “${q}”` : `No ${STATUS_LABEL[status].toLowerCase()} books`}
          action={<button className="btn" onClick={() => setParams(new URLSearchParams(sort === "title" ? {} : { sort }), { replace: true })}>Clear filters</button>}
        >
          {q ? "Try a shorter word, an author’s surname, or another status." : "Books move here as KOReader reports progress."}
        </EmptyState>
      ) : view === "grid" ? (
        <ul className="book-grid" aria-label="Books">
          {filtered.map((w) => (
            <li key={w.id}>
              <button className="book" style={tintStyle(w)} onClick={() => open(w)} aria-label={`${w.title} by ${w.author}${w.progress ? `, ${percent(w.progress.percentage)} read` : ""}`}>
                <span className="cover-wrap">
                  <Cover work={w} />
                  {w.progress && <ProgressRing value={w.progress.percentage} />}
                </span>
                <strong><Highlight text={w.title} q={q} /></strong>
                <span><Highlight text={w.author} q={q} /></span>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <ul className="book-list" aria-label="Books">
          {filtered.map((w) => (
            <li key={w.id}>
              <button className="book" style={tintStyle(w)} onClick={() => open(w)}>
                <Cover work={w} />
                <span className="titles">
                  <strong><Highlight text={w.title} q={q} /></strong>
                  <span><Highlight text={w.author} q={q} /></span>
                </span>
                <span className="when">{w.progress ? `${percent(w.progress.percentage)} · ${relativeTime(w.progress.updated_at)}` : `added ${relativeTime(w.created_at)}`}</span>
                {w.progress ? <ProgressRing value={w.progress.percentage} size={30} /> : <span />}
              </button>
            </li>
          ))}
        </ul>
      )}

      <Sheet open={!!selected} onClose={() => nav(`/app/library${loc.search}`)} title={selected?.title ?? ""}>
        {selected && <WorkDetail work={selected} />}
      </Sheet>
    </>
  );
}

const GridIcon = () => (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><rect x="4" y="4" width="7" height="7" rx="1.5" /><rect x="13" y="4" width="7" height="7" rx="1.5" /><rect x="4" y="13" width="7" height="7" rx="1.5" /><rect x="13" y="13" width="7" height="7" rx="1.5" /></svg>
);
const ListIcon = () => (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01" /></svg>
);

function WorkDetail({ work }: { work: Work }) {
  const p = work.progress;
  const showTech = !!useStore().me?.preferences.show_technical_details;
  return (
    <div className="stack" style={tintStyle(work)}>
      <div style={{ display: "grid", gridTemplateColumns: "140px 1fr", gap: 18 }}>
        <Cover work={work} />
        <div className="stack" style={{ gap: 10 }}>
          <div className="hint" style={{ fontSize: 15, color: "var(--ink-2)" }}>{work.author}</div>
          <div className="row">
            {work.language && <span className="badge">{work.language}</span>}
            {work.published && <span className="badge">{work.published.slice(0, 4)}</span>}
            {work.isbn && <span className="badge">ISBN {work.isbn}</span>}
            {work.publisher && <span className="badge">{work.publisher}</span>}
          </div>
          {p ? (
            <div>
              <div className="progress-bar"><span style={{ width: `${Math.round(p.percentage * 100)}%` }} /></div>
              <div className="hint" style={{ marginTop: 6 }}>
                <strong style={{ color: "var(--tint)" }}>{percent(p.percentage)}</strong> · {p.device || p.sync_key_label || "device"} · {relativeTime(p.updated_at)}
              </div>
            </div>
          ) : (
            <div className="hint">Not started on any synced device.</div>
          )}
        </div>
      </div>
      {work.description && <p style={{ margin: 0, color: "var(--ink-2)" }}>{work.description.length > 900 ? work.description.slice(0, 900) + "…" : work.description}</p>}
      <div>
        <h3 style={{ margin: "0 0 8px", fontSize: 14, textTransform: "uppercase", letterSpacing: ".06em", color: "var(--muted)" }}>Files</h3>
        <table className="list">
          <tbody>
            {work.files.map((f) => (
              <tr key={f.id}>
                <td>
                  <strong>EPUB</strong>
                  <div className="hint">{bytes(f.size)} · added {relativeTime(f.added_at)}</div>
                  {showTech && f.partial_md5 && <div className="hint" style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>Document id {f.partial_md5}</div>}
                  {f.progress && <div className="hint">{percent(f.progress.percentage)} synced {absoluteTime(f.progress.updated_at)}</div>}
                </td>
                <td style={{ textAlign: "right" }}>
                  <a className="btn sm" href={f.download_url}><Download /> Download</a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="hint">
        {showTech && <>Stored at <code style={{ fontFamily: "var(--font-mono)" }}>{work.dir_path}</code>. </>}Add it to KOReader from the OPDS catalogue on a <Link to="/app/devices">connected device</Link>.
      </div>
    </div>
  );
}
