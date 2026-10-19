import { useEffect, useMemo, useRef } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Cover, tintStyle } from "../components/Cover";
import { EmptyState } from "../components/EmptyState";
import { Book, Download, Search as SearchIcon } from "../components/Icons";
import { Sheet } from "../components/Sheet";
import { PageTitle } from "../components/Shell";
import { absoluteTime, bytes, percent, relativeTime } from "../lib/format";
import { useHotkeys, useLocalStorage } from "../lib/hooks";
import { useStore } from "../lib/store";
import type { Work } from "../lib/types";

type Sort = "title" | "author" | "added";

function norm(s: string) {
  return s.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

function Highlight({ text, q }: { text: string; q: string }) {
  if (!q) return <>{text}</>;
  const i = norm(text).indexOf(norm(q));
  if (i < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, i)}
      <mark>{text.slice(i, i + q.length)}</mark>
      {text.slice(i + q.length)}
    </>
  );
}

export function LibraryPage() {
  const { works, worksLoaded, me, refreshLibrary } = useStore();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const sort = (params.get("sort") as Sort) || "title";
  const [view, setView] = useLocalStorage<"grid" | "list">("lectern.libraryView", "grid");
  const { id } = useParams();
  const nav = useNavigate();
  const input = useRef<HTMLInputElement>(null);

  useHotkeys([{ keys: "f", handler: () => input.current?.focus() }]);
  useEffect(() => {
    refreshLibrary().catch(() => {});
  }, [refreshLibrary]);

  const filtered = useMemo(() => {
    const needle = norm(q.trim());
    const list = needle ? works.filter((w) => norm(`${w.title} ${w.author}`).includes(needle)) : works.slice();
    const by: Record<Sort, (a: Work, b: Work) => number> = {
      title: (a, b) => a.sort_title.localeCompare(b.sort_title),
      author: (a, b) => a.sort_author.localeCompare(b.sort_author) || a.sort_title.localeCompare(b.sort_title),
      added: (a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""),
    };
    return list.sort(by[sort]);
  }, [works, q, sort]);

  const selected = id ? works.find((w) => w.id === Number(id)) ?? null : null;
  const setQ = (v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set("q", v);
    else next.delete("q");
    setParams(next, { replace: true });
  };
  const setSort = (s: Sort) => {
    const next = new URLSearchParams(params);
    if (s === "title") next.delete("sort");
    else next.set("sort", s);
    setParams(next, { replace: true });
  };

  return (
    <>
      <PageTitle>Library</PageTitle>
      <div className="filter-bar" role="search">
        <label className="sr-only" htmlFor="lib-q">Filter by title or author</label>
        <input
          id="lib-q"
          ref={input}
          className="input"
          placeholder="Filter by title or author  (f)"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Escape" && (setQ(""), input.current?.blur())}
        />
        <div className="seg" role="group" aria-label="Sort">
          {(["title", "author", "added"] as Sort[]).map((s) => (
            <button key={s} aria-pressed={sort === s} onClick={() => setSort(s)}>
              {s === "added" ? "Newest" : s[0].toUpperCase() + s.slice(1)}
            </button>
          ))}
        </div>
        <div className="seg" role="group" aria-label="View">
          <button aria-pressed={view === "grid"} onClick={() => setView("grid")}>Grid</button>
          <button aria-pressed={view === "list"} onClick={() => setView("list")}>List</button>
        </div>
        <span className="count" aria-live="polite">
          {q ? `${filtered.length} of ${works.length}` : `${works.length} ${works.length === 1 ? "work" : "works"}`}
        </span>
      </div>

      {!worksLoaded ? (
        <div className="hint">Loading…</div>
      ) : works.length === 0 ? (
        <EmptyState icon={<Book />} title="No books yet">
          Drop EPUB files into <code>{me?.inbox_path ?? "the inbox"}</code>. Lectern reads their metadata, files them under author and title, and lists them here.
        </EmptyState>
      ) : filtered.length === 0 ? (
        <EmptyState icon={<SearchIcon />} title={`Nothing matches “${q}”`} action={<button className="btn" onClick={() => setQ("")}>Clear filter</button>}>
          Try a shorter word, or search by author.
        </EmptyState>
      ) : view === "grid" ? (
        <div className="book-grid">
          {filtered.map((w) => (
            <button key={w.id} className="book" style={tintStyle(w)} onClick={() => nav(`/app/library/${w.id}${location.search}`)} aria-label={`${w.title} by ${w.author}`}>
              <Cover work={w} />
              <strong><Highlight text={w.title} q={q} /></strong>
              <span><Highlight text={w.author} q={q} /></span>
              {w.progress && (
                <div className="progress-bar" title={`${percent(w.progress.percentage)} read`}>
                  <span style={{ width: `${Math.round(w.progress.percentage * 100)}%` }} />
                </div>
              )}
            </button>
          ))}
        </div>
      ) : (
        <div className="book-list">
          {filtered.map((w) => (
            <button key={w.id} className="book" style={tintStyle(w)} onClick={() => nav(`/app/library/${w.id}${location.search}`)}>
              <Cover work={w} />
              <div>
                <strong><Highlight text={w.title} q={q} /></strong>
                <br />
                <span><Highlight text={w.author} q={q} /></span>
              </div>
              <span>{w.progress ? `${percent(w.progress.percentage)} · ${relativeTime(w.progress.updated_at)}` : relativeTime(w.created_at)}</span>
            </button>
          ))}
        </div>
      )}

      <Sheet open={!!selected} onClose={() => nav(`/app/library${location.search}`)} title={selected?.title ?? ""}>
        {selected && <WorkDetail work={selected} />}
      </Sheet>
    </>
  );
}

function WorkDetail({ work }: { work: Work }) {
  const p = work.progress;
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
                  <strong>{f.rel_path}</strong>
                  <div className="hint">{bytes(f.size)} · added {relativeTime(f.added_at)}</div>
                  {f.partial_md5 && <div className="hint" style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>KOReader id {f.partial_md5}</div>}
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
        Stored at <code style={{ fontFamily: "var(--font-mono)" }}>{work.dir_path}</code>. Add it to KOReader from the OPDS catalogue on a <Link to="/app/devices">connected device</Link>.
      </div>
    </div>
  );
}
