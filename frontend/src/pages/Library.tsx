import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Cover, tintStyle } from "../components/Cover";
import { EmptyState } from "../components/EmptyState";
import { NeedsEpub, ReadAloudFormats } from "../components/ReadAloudFormats";
import { Book, Close, Down, Download, Folder, Headphones, Pencil, Plus, Reader, Search as SearchIcon, Select, Send, Shelf as ShelfIcon, Upload } from "../components/Icons";
import { SendToDevice } from "../components/SendToDevice";
import { OverflowMenu } from "../components/Menu";
import { BulkBar, type BulkTarget } from "../components/BulkBar";
import { del, get, patch, post, put } from "../lib/api";
import { clock, usePlayer } from "../lib/player";
import type { MatchSuggestion, Shelf, ShelfGrant, User } from "../lib/types";
import { fold } from "../lib/fuzzy";
import { Sheet } from "../components/Sheet";
import { PageTitle } from "../components/Shell";
import { absoluteTime, bytes, percent, relativeTime } from "../lib/format";
import { MOD, useLocalStorage } from "../lib/hooks";
import { useSelection, type CellProps } from "../lib/useSelection";
import { useGridColumns, useWindowRows } from "../lib/virtual";
import { useStore } from "../lib/store";
import { bookFiles, formatName, formatOf, hasEpub, isPaged, readerPath } from "../lib/formats";
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
  const { works, worksLoaded, me, refreshLibrary, canUpload, canEdit, pickBooks, pickFolder, toast, imports, setReviewOpen } = useStore();
  const [sendingMany, setSendingMany] = useState(false);
  const readyImports = imports.filter((i) => i.status === "ready").length;
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const sort = (params.get("sort") as Sort) || "title";
  const status = (params.get("status") as Status) || "all";
  const [view, setView] = useLocalStorage<"grid" | "list">("lectern.libraryView", "grid");
  const { id } = useParams();
  const nav = useNavigate();
  const loc = useLocation();
  const input = useRef<HTMLInputElement>(null);
  const shelfId = params.get("shelf");
  const [shelves, setShelves] = useState<Shelf[]>([]);
  const [canCreateShelf, setCanCreateShelf] = useState(false);
  const [shelfWorks, setShelfWorks] = useState<Set<number> | null>(null);
  const [suggestions, setSuggestions] = useState<MatchSuggestion[]>([]);
  const [manageShelves, setManageShelves] = useState(false);
  const review = params.get("review") === "1";

  const loadShelves = useCallback(async () => {
    const d = await get<{ shelves: Shelf[]; can_create: boolean }>("/api/shelves");
    setShelves(d.shelves);
    setCanCreateShelf(d.can_create);
  }, []);
  const loadSuggestions = useCallback(async () => {
    if (!canUpload) return;
    setSuggestions((await get<{ suggestions: MatchSuggestion[] }>("/api/suggestions")).suggestions);
  }, [canUpload]);

  useEffect(() => {
    refreshLibrary().catch(() => {});
    loadShelves().catch(() => {});
  }, [refreshLibrary, loadShelves]);
  useEffect(() => {
    loadSuggestions().catch(() => {});
  }, [loadSuggestions, works.length]);
  useEffect(() => {
    if (!shelfId) return setShelfWorks(null);
    get<{ works: Work[] }>(`/api/shelves/${shelfId}`)
      .then((d) => setShelfWorks(new Set(d.works.map((w) => w.id))))
      .catch(() => setShelfWorks(new Set()));
  }, [shelfId, shelves]);

  const counts = useMemo(() => {
    const c: Record<Status, number> = { all: works.length, reading: 0, unread: 0, finished: 0 };
    for (const w of works) c[statusOf(w)] += 1;
    return c;
  }, [works]);

  const filtered = useMemo(() => {
    const needle = fold(q.trim());
    let list = needle ? works.filter((w) => fold(`${w.title} ${w.author} ${w.isbn ?? ""}`).includes(needle)) : works.slice();
    if (shelfWorks) list = list.filter((w) => shelfWorks.has(w.id));
    if (status !== "all") list = list.filter((w) => statusOf(w) === status);
    const by: Record<Sort, (a: Work, b: Work) => number> = {
      title: (a, b) => a.sort_title.localeCompare(b.sort_title),
      author: (a, b) => a.sort_author.localeCompare(b.sort_author) || a.sort_title.localeCompare(b.sort_title),
      added: (a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""),
    };
    return list.sort(by[sort]);
  }, [works, q, sort, status, shelfWorks]);

  const selected = id ? works.find((w) => w.id === Number(id)) ?? null : null;
  const setParam = (k: string, v: string, dflt: string) => {
    const next = new URLSearchParams(params);
    if (v && v !== dflt) next.set(k, v);
    else next.delete(k);
    setParams(next, { replace: true });
  };
  const open = (w: Work) => nav(`/app/library/${w.id}${loc.search}`);
  const filtering = !!q || status !== "all" || !!shelfId;
  const reader = me?.user.role === "reader";

  // Multi-select over the filtered view (lib/useSelection). The collection is windowed: only
  // rows near the viewport are rendered, and Cmd/Ctrl+A selects the whole view on the server.
  const [scope, setScope] = useState<HTMLDivElement | null>(null);
  const grid = view === "grid";
  const keys = useMemo(() => filtered.map((w) => w.id), [filtered]);
  const { columns, width } = useGridColumns(scope, 136, 20, grid);
  const rowGap = grid ? 26 : 2;
  const win = useWindowRows(scope, { count: keys.length, columns, gap: rowGap, estimate: grid ? 280 : 58, width });
  const viewFilter = useMemo(() => ({ q: q.trim(), status, shelf: shelfId ? Number(shelfId) : null }), [q, status, shelfId]);
  // The view as the server counted it when everything was selected: its size and which works
  // they are. Kept as the user saw it ("All N selected"); the server refuses the action if the
  // view no longer holds exactly those works.
  const [viewInfo, setViewInfo] = useState<{ total: number; view_token: string } | null>(null);
  const viewTotal = viewInfo?.total ?? null;
  const s = useSelection<number>({
    scope,
    keys,
    columns,
    total: viewTotal ?? keys.length,
    onOpen: (id) => nav(`/app/library/${id}${loc.search}`),
    reveal: win.reveal,
    rendered: [win.start * columns, Math.min(keys.length, win.end * columns)],
    resetOn: JSON.stringify(viewFilter),
    global: true,
    marquee: true,
  });
  // "All N selected" counts the view on the server: it may hold more than the client loaded.
  useEffect(() => {
    if (!s.sel.all) return setViewInfo(null);
    const p = new URLSearchParams({ q: viewFilter.q, status: viewFilter.status });
    if (viewFilter.shelf !== null) p.set("shelf", String(viewFilter.shelf));
    let stale = false;
    get<{ total: number; view_token: string }>(`/api/library/count?${p}`).then((d) => !stale && setViewInfo(d)).catch((e) => toast((e as Error).message, "error"));
    return () => {
      stale = true;
    };
  }, [s.sel.all, viewFilter, toast]);
  /** Explicit ids, or for "all" the view the user saw minus what they deselected; null while
   * that view is still being counted. */
  const target = (): BulkTarget | null => {
    if (!s.sel.all) return { work_ids: s.selectedKeys() };
    if (!viewInfo) return null;
    return { selection: { filter: viewFilter, exclude_ids: [...s.sel.set], view_token: viewInfo.view_token, expected_count: s.count } };
  };
  const pickedTitles = useMemo(() => filtered.filter((w) => s.isSelected(w.id)).map((w) => w.title), [filtered, s]);
  const rowsShown = Array.from({ length: win.end - win.start }, (_, i) => win.start + i);

  return (
    <>
      <PageTitle
        sub={worksLoaded ? `${works.length} ${works.length === 1 ? "book" : "books"} · ${counts.reading} reading · ${counts.finished} finished` : undefined}
        aside={
          <>
            {(canCreateShelf || shelves.length > 0) && <button className="btn ghost" onClick={() => setManageShelves(true)}><ShelfIcon /> Shelves</button>}
            {works.length > 0 && (
              <button className="btn ghost" aria-pressed={s.selecting} onClick={() => (s.selecting ? s.exit() : s.setSelecting(true))} title={`Click to select, ${MOD}-click to add, Shift-click for a range, ${MOD} A for all`}>
                <Select /> {s.selecting ? "Done" : "Select"}
              </button>
            )}
            {canUpload && (
              <span className="split-btn">
                <button className="btn primary" onClick={pickBooks}><Plus /> Add books</button>
                <OverflowMenu
                  label="More ways to add books"
                  className="btn primary split-more"
                  trigger={<Down />}
                  actions={[
                    { label: "Choose files (EPUB, PDF, CBZ, M4B, MP3)…", onSelect: pickBooks },
                    { label: "Choose a folder (one audiobook)…", onSelect: pickFolder },
                    ...(readyImports ? [{ label: `Review ${readyImports} waiting`, onSelect: () => setReviewOpen(true) }] : []),
                  ]}
                />
              </span>
            )}
          </>
        }
      >
        Library
      </PageTitle>
      {readyImports > 0 && (
        <div className="alert review-banner" role="status">
          <span><strong>{readyImports === 1 ? "One book" : `${readyImports} books`}</strong> uploaded and waiting for you to check the details before filing.</span>
          <button className="btn sm primary" onClick={() => setReviewOpen(true)}>Review</button>
        </div>
      )}
      {suggestions.length > 0 && (
        <div className="alert review-banner" role="status">
          <span><strong>{suggestions.length === 1 ? "One upload" : `${suggestions.length} uploads`}</strong> might belong to a book you already have.</span>
          <button className="btn sm" onClick={() => setParam("review", "1", "")}>Review</button>
        </div>
      )}
      {shelves.length > 0 && (
        <div className="chips shelf-chips" role="group" aria-label="Shelves">
          {!reader && <button className="chip subtle" aria-pressed={!shelfId} onClick={() => setParam("shelf", "", "")}>Everything</button>}
          {shelves.map((sh) => (
            <button key={sh.id} className="chip subtle" aria-pressed={shelfId === String(sh.id)} onClick={() => setParam("shelf", shelfId === String(sh.id) ? "" : String(sh.id), "")}>
              <ShelfIcon width={14} height={14} /> {sh.name} <span className="n">{sh.work_count}</span>
            </button>
          ))}
        </div>
      )}
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
        reader ? (
          <EmptyState icon={<ShelfIcon />} title="No shelves shared with you yet">
            Your household shares books with you shelf by shelf. Ask the owner to add you to one.
          </EmptyState>
        ) : (
          <EmptyState
            icon={<Book />}
            title="No books yet"
            action={canUpload ? <div className="row"><button className="btn primary" onClick={pickBooks}><Upload /> Add books</button><button className="btn" onClick={pickFolder}><Folder /> Add an audiobook folder</button></div> : undefined}
          >
            Drop EPUBs and audiobooks (M4B, or a folder of MP3s) anywhere on this page, or put them in <code>{me?.inbox_path ?? "the inbox"}</code>. You check the details before anything is filed.
          </EmptyState>
        )
      ) : filtered.length === 0 ? (
        <EmptyState
          icon={<SearchIcon />}
          title={q ? `Nothing matches “${q}”` : `No ${STATUS_LABEL[status].toLowerCase()} books`}
          action={<button className="btn" onClick={() => setParams(new URLSearchParams(sort === "title" ? {} : { sort }), { replace: true })}>Clear filters</button>}
        >
          {q ? "Try a shorter word, an author’s surname, or another status." : shelfId ? "This shelf has nothing in this view yet." : "Books move here as KOReader reports progress."}
        </EmptyState>
      ) : (
        <div
          ref={setScope}
          className={grid ? "book-grid" : "book-list"}
          role="grid"
          aria-label={shelfId ? `Books on ${shelves.find((x) => String(x.id) === shelfId)?.name ?? "this shelf"}` : q ? `Books matching ${q}` : "Books"}
          aria-multiselectable="true"
          aria-rowcount={win.rows}
          aria-colcount={columns}
          data-has-selection={s.count > 0 || s.selecting || undefined}
          style={{ paddingTop: win.padTop, paddingBottom: win.padBottom }}
          {...s.scopeProps}
        >
          {rowsShown.map((r) => (
            <div key={r} role="row" aria-rowindex={r + 1} data-vrow className="vrow" style={{ gridTemplateColumns: grid ? `repeat(${columns}, minmax(0, 1fr))` : undefined, height: win.stride - rowGap, marginBottom: rowGap }}>
              {filtered.slice(r * columns, (r + 1) * columns).map((w, c) => (
                <BookCell key={w.id} work={w} q={q} grid={grid} cell={s.cellProps(w.id)} col={c + 1} />
              ))}
            </div>
          ))}
          {s.marquee && <div className="marquee" style={s.marquee} aria-hidden="true" />}
        </div>
      )}

      <BulkBar
          open={s.count > 0 || s.selecting}
          ready={!s.sel.all || !!viewInfo}
          count={s.count}
          all={s.allSelected}
          total={viewTotal ?? keys.length}
          target={target}
          titles={pickedTitles}
          shelves={shelves.filter((sh) => sh.permission !== "view" && !sh.smart)}
          onSelectAll={s.selectAll}
          onClear={s.exit}
          onDone={async () => {
            await Promise.all([refreshLibrary(), loadShelves()]).catch(() => {});
          }}
          canEdit={canEdit}
          canAlign={canEdit}
          onSend={() => setSendingMany(true)}
        />
      <SendToDevice
        open={sendingMany}
        target={() => target() ?? { work_ids: [] }}
        count={s.count}
        title={`Send ${s.count} ${s.count === 1 ? "book" : "books"} to a device`}
        onClose={() => setSendingMany(false)}
        onSent={s.exit}
      />
      <Sheet open={!!selected} onClose={() => nav(`/app/library${loc.search}`)} title={selected?.title ?? ""}>
        {selected && <WorkDetail work={selected} shelves={shelves} onShelvesChanged={() => loadShelves().catch(() => {})} />}
      </Sheet>
      <Sheet open={manageShelves} onClose={() => setManageShelves(false)} title="Shelves" labelledBy="shelves-title">
        <ShelvesManager shelves={shelves} canCreate={canCreateShelf} reload={() => loadShelves().catch(() => {})} />
      </Sheet>
      <Sheet open={review} onClose={() => setParam("review", "", "")} title="Review matches" labelledBy="review-title">
        <SuggestionReview
          items={suggestions}
          onResolved={async (msg) => {
            toast(msg);
            await Promise.all([loadSuggestions(), refreshLibrary()]).catch(() => {});
          }}
        />
      </Sheet>
    </>
  );
}

/** One book in the Library grid or list: a grid cell, selected with the pointer or keyboard
 * (lib/useSelection), opened with Enter, a double-click, or a tap. */
function BookCell({ work: w, q, grid, cell, col }: { work: Work; q: string; grid: boolean; cell: CellProps; col: number }) {
  const label = `${w.title} by ${w.author}${w.progress ? `, ${percent(w.progress.percentage)} read` : ""}`;
  const pick = (
    <span className="pick" data-sel-toggle aria-hidden="true" title="Select">
      {cell["aria-selected"] ? "✓" : ""}
    </span>
  );
  return grid ? (
    <div className="book" style={tintStyle(w)} aria-label={label} aria-colindex={col} {...cell}>
      {pick}
      <span className="cover-wrap">
        <Cover work={w} />
        {w.progress && <ProgressRing value={w.progress.percentage} />}
        {w.cover_generated && w.audio && <span className="audio-badge"><Headphones aria-label="Has audiobook" /></span>}
      </span>
      {/* A generated cover already shows the title and author; a caption would repeat them.
          The cell's aria-label still names the book. */}
      {!w.cover_generated && (
        <>
          <strong><Highlight text={w.title} q={q} /></strong>
          <span><Highlight text={w.author} q={q} />{w.audio && <Headphones className="has-audio" aria-label="Has audiobook" />}</span>
        </>
      )}
    </div>
  ) : (
    <div className="book" style={tintStyle(w)} aria-label={label} aria-colindex={col} {...cell}>
      {pick}
      <Cover work={w} />
      <span className="titles">
        <strong><Highlight text={w.title} q={q} /></strong>
        <span><Highlight text={w.author} q={q} /></span>
      </span>
      <span className="when">{w.progress ? `${percent(w.progress.percentage)} · ${relativeTime(w.progress.updated_at)}` : `added ${relativeTime(w.created_at)}`}</span>
      {w.progress ? <ProgressRing value={w.progress.percentage} size={30} /> : <span />}
    </div>
  );
}

const GridIcon = () => (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><rect x="4" y="4" width="7" height="7" rx="1.5" /><rect x="13" y="4" width="7" height="7" rx="1.5" /><rect x="4" y="13" width="7" height="7" rx="1.5" /><rect x="13" y="13" width="7" height="7" rx="1.5" /></svg>
);
const ListIcon = () => (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01" /></svg>
);

function WorkDetail({ work, shelves, onShelvesChanged }: { work: Work; shelves: Shelf[]; onShelvesChanged: () => void }) {
  const p = work.progress;
  const { me, canUpload, canEdit, works, toast, refreshLibrary, now, openEditor } = useStore();
  const loc = useLocation();
  const player = usePlayer();
  const nav = useNavigate();
  const showTech = !!me?.preferences.show_technical_details;
  const books = bookFiles(work);
  const ebook = books[0];
  const epub = hasEpub(work);
  const pos = work.position;
  const editable = shelves.filter((sh) => sh.permission !== "view" && !sh.smart);
  const [member, setMember] = useState<Set<number> | null>(null);
  const [sending, setSending] = useState(false);
  const [merging, setMerging] = useState(false);

  useEffect(() => {
    if (!editable.length) return;
    Promise.all(editable.map((sh) => get<{ works: Work[] }>(`/api/shelves/${sh.id}`).then((d) => [sh.id, d.works.some((w) => w.id === work.id)] as const)))
      .then((pairs) => setMember(new Set(pairs.filter(([, on]) => on).map(([id]) => id))))
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [work.id, shelves.length]);

  const toggleShelf = async (sh: Shelf) => {
    const on = member?.has(sh.id);
    try {
      if (on) await del(`/api/shelves/${sh.id}/items/${work.id}`);
      else await post(`/api/shelves/${sh.id}/items`, { work_id: work.id });
      setMember((m) => {
        const n = new Set(m);
        if (on) n.delete(sh.id);
        else n.add(sh.id);
        return n;
      });
      onShelvesChanged();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

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
            {work.audio && <span className="badge"><Headphones width={13} height={13} /> {clock(work.audio.duration_s)}</span>}
          </div>
          {(work.series || (work.narrators?.length ?? 0) > 0 || (work.tags?.length ?? 0) > 0) && (
            <div className="hint">
              {work.series && <>{work.series}{work.series_index != null ? ` #${work.series_index}` : ""}</>}
              {work.series && (work.narrators?.length ?? 0) > 0 && " · "}
              {(work.narrators?.length ?? 0) > 0 && <>Read by {work.narrators!.join(", ")}</>}
              {(work.tags?.length ?? 0) > 0 && <div className="chips" style={{ marginTop: 6 }}>{work.tags!.map((t) => <span key={t} className="badge">{t}</span>)}</div>}
            </div>
          )}
          <div className="row">
            {ebook && <button className="btn primary" onClick={() => nav(readerPath(work))}><Reader /> {pos?.source === "web" || p ? "Continue reading" : "Read"}</button>}
            {work.audio && (
              <button className={`btn ${ebook ? "" : "primary"}`} onClick={() => { player.open(work, { autoplay: true }); }}>
                <Headphones /> {pos?.audio_s ? `Listen from ${clock(pos.audio_s)}` : "Listen"}
              </button>
            )}
            {canEdit && (
              <button className="btn ghost" onClick={() => { nav(`/app/library${loc.search}`); openEditor(work.id); }}>
                <Pencil /> Edit
              </button>
            )}
            {ebook && <button className="btn ghost" onClick={() => setSending(true)}><Send /> Send to device</button>}
          </div>
          <SendToDevice open={sending} workIds={[work.id]} title={`Send “${work.title}” to a device`} onClose={() => setSending(false)} />
          {pos && (
            <div>
              <div className="progress-bar"><span style={{ width: `${Math.round((pos.fraction ?? 0) * 100)}%` }} /></div>
              <div className="hint" style={{ marginTop: 6 }}>
                <strong style={{ color: "var(--tint)" }}>{percent(pos.fraction)}</strong> · {pos.source === "audio" ? "listening" : "reading"} on {pos.device || "the web"} · {relativeTime(pos.received_at, now)}
              </div>
            </div>
          )}
          {p ? (
            <div>
              {!pos && <div className="progress-bar"><span style={{ width: `${Math.round(p.percentage * 100)}%` }} /></div>}
              <div className="hint" style={{ marginTop: 6 }}>
                {/^\d+$/.test(p.progress) ? `Page ${p.progress}` : "KOReader"}: <strong style={{ color: "var(--tint)" }}>{percent(p.percentage)}</strong> · {p.device || p.sync_key_label || "device"} · {relativeTime(p.updated_at, now)}
              </div>
            </div>
          ) : (
            !pos && <div className="hint">Not started yet.</div>
          )}
        </div>
      </div>
      {work.description && <p style={{ margin: 0, color: "var(--ink-2)" }}>{work.description.length > 900 ? work.description.slice(0, 900) + "…" : work.description}</p>}
      <div>
        <h3 className="detail-h">Files</h3>
        <table className="list">
          <tbody>
            {books.map((f) => (
              <tr key={f.id}>
                <td>
                  <strong>{formatName(f)}</strong>
                  <div className="hint">{bytes(f.size)} · added {relativeTime(f.added_at)}</div>
                  {showTech && f.partial_md5 && <div className="hint mono">Document id {f.partial_md5}</div>}
                  {f.progress && <div className="hint">{percent(f.progress.percentage)} synced {absoluteTime(f.progress.updated_at)}</div>}
                </td>
                <td style={{ textAlign: "right" }}>
                  <span className="row" style={{ gap: 4, justifyContent: "flex-end" }}>
                    {isPaged(f) && <Link className="btn sm ghost" to={readerPath(work, f)} aria-label={`Read the ${formatOf(f)} of ${work.title}`}><Reader /> Read</Link>}
                    <a className="btn sm" href={f.download_url}><Download /> Download</a>
                  </span>
                </td>
              </tr>
            ))}
            {work.audio && (
              <tr>
                <td>
                  <strong>Audiobook</strong>
                  <div className="hint">{work.audio.tracks} {work.audio.tracks === 1 ? "file" : "files"} · {clock(work.audio.duration_s)}</div>
                </td>
                <td style={{ textAlign: "right" }}>
                  <Link className="btn sm" to="/app/listening" onClick={() => { if (player.work?.id !== work.id) player.open(work); }}>Files and downloads</Link>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div>
        <h3 style={{ margin: "0 0 8px", fontSize: 14, textTransform: "uppercase", letterSpacing: ".06em", color: "var(--muted)" }}>Read-along</h3>
        <ul className="slots">
          {epub ? <ReadAloudFormats work={work} active /> : <NeedsEpub />}
        </ul>
      </div>
      {editable.length > 0 && (
        <div>
          <h3 className="detail-h">Shelves</h3>
          <div className="chips" role="group" aria-label="Shelves for this book">
            {editable.map((sh) => (
              <button key={sh.id} className="chip subtle" aria-pressed={!!member?.has(sh.id)} onClick={() => toggleShelf(sh)} disabled={!member}>
                {member?.has(sh.id) ? "✓ " : "+ "}{sh.name}
              </button>
            ))}
          </div>
        </div>
      )}
      {canUpload && (
        <div>
          {merging ? (
            <MergeForm work={work} works={works} onCancel={() => setMerging(false)} onDone={async (target) => {
              setMerging(false);
              toast(`Merged into ${target.title}`);
              await refreshLibrary();
              nav(`/app/library/${target.id}`);
            }} />
          ) : (
            <button className="btn sm ghost" onClick={() => setMerging(true)}>Same book as another entry? Merge…</button>
          )}
        </div>
      )}
      <div className="hint">
        {showTech && <>Stored at <code className="mono">{work.dir_path}</code>. </>}Add it to KOReader from the OPDS catalogue on a <Link to="/app/devices">connected device</Link>.
      </div>
    </div>
  );
}

function MergeForm({ work, works, onCancel, onDone }: { work: Work; works: Work[]; onCancel: () => void; onDone: (target: Work) => void }) {
  const { toast } = useStore();
  const [target, setTarget] = useState("");
  const options = works.filter((w) => w.id !== work.id).sort((a, b) => a.sort_title.localeCompare(b.sort_title));
  return (
    <form
      className="merge-form"
      onSubmit={async (e) => {
        e.preventDefault();
        const t = options.find((w) => String(w.id) === target);
        if (!t) return;
        try {
          await post(`/api/works/${work.id}/merge`, { target_work_id: t.id });
          onDone(t);
        } catch (ex) {
          toast((ex as Error).message, "error");
        }
      }}
    >
      <label className="field">
        <span>Move this entry’s files into</span>
        <select className="select" value={target} onChange={(e) => setTarget(e.target.value)} required data-autofocus>
          <option value="">Choose a book…</option>
          {options.map((w) => <option key={w.id} value={w.id}>{w.title} — {w.author}{w.audio ? " (audio)" : ""}</option>)}
        </select>
      </label>
      <p className="hint">“{work.title}” disappears and its EPUB or audio joins the chosen book. Reading positions follow.</p>
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button type="button" className="btn ghost" onClick={onCancel}>Cancel</button>
        <button className="btn primary" disabled={!target}>Merge</button>
      </div>
    </form>
  );
}

function SuggestionReview({ items, onResolved }: { items: MatchSuggestion[]; onResolved: (msg: string) => Promise<void> }) {
  const { toast } = useStore();
  if (!items.length) return <p className="hint">Nothing to review. Uploads that only loosely match a book you have show up here.</p>;
  const act = async (s: MatchSuggestion, what: "merge" | "dismiss") => {
    try {
      await post(`/api/suggestions/${s.id}/${what}`);
      await onResolved(what === "merge" ? `Merged into ${s.candidate.title}` : `Kept “${s.work.title}” separate`);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  const label = (w: Work) => [[...new Set(bookFiles(w).map(formatOf))].join(", ") || null, w.audio ? `audio ${clock(w.audio.duration_s)}` : null].filter(Boolean).join(" + ");
  return (
    <ul className="review-list">
      {items.map((s) => (
        <li key={s.id} className="card">
          <p className="sub" style={{ marginTop: 0 }}>Is the new upload the same book? <span className="badge">{Math.round(s.score)}% match · {s.reason}</span></p>
          <div className="pair">
            <div style={tintStyle(s.work)}>
              <Cover work={s.work} />
              <strong>{s.work.title}</strong>
              <span className="hint">{s.work.author} · {label(s.work)} · new</span>
            </div>
            <div style={tintStyle(s.candidate)}>
              <Cover work={s.candidate} />
              <strong>{s.candidate.title}</strong>
              <span className="hint">{s.candidate.author} · {label(s.candidate)}</span>
            </div>
          </div>
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <button className="btn ghost" onClick={() => act(s, "dismiss")}>Different books</button>
            <button className="btn primary" onClick={() => act(s, "merge")}>Same book — merge</button>
          </div>
        </li>
      ))}
    </ul>
  );
}

function ShelvesManager({ shelves, canCreate, reload }: { shelves: Shelf[]; canCreate: boolean; reload: () => void }) {
  const { toast, me } = useStore();
  const [name, setName] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await post("/api/shelves", { name: name.trim() });
      setName("");
      reload();
    } catch (ex) {
      toast((ex as Error).message, "error");
    }
  };
  return (
    <div className="stack">
      <p className="sub" style={{ margin: 0 }}>
        {me?.user.role === "reader"
          ? "Shelves the household shares with you. You see only the books on them."
          : "Group books, and share a shelf with readers in your household: readers see only the shelves they are given."}
      </p>
      {canCreate && (
        <form className="row" onSubmit={create}>
          <label className="sr-only" htmlFor="new-shelf">New shelf name</label>
          <input id="new-shelf" className="input" style={{ flex: 1 }} placeholder="New shelf, e.g. Kids" value={name} onChange={(e) => setName(e.target.value)} maxLength={128} required />
          <button className="btn primary"><Plus /> Create</button>
        </form>
      )}
      {shelves.length === 0 ? (
        <p className="hint">No shelves yet.</p>
      ) : (
        <ul className="shelf-list">
          {shelves.map((sh) => (
            <li key={sh.id}>
              <div className="shelf-row">
                <ShelfIcon />
                <div>
                  <strong>{sh.name}</strong>
                  <div className="hint">
                    {sh.work_count} {sh.work_count === 1 ? "book" : "books"}
                    {sh.permission === "owner" ? ` · shared with ${sh.grant_count}` : ` · ${sh.owner ? `@${sh.owner}’s` : "shared"} · you can ${sh.permission}`}
                  </div>
                </div>
                {sh.permission === "owner" && (
                  <>
                    <button className="btn sm ghost" aria-expanded={open === sh.id} onClick={() => setOpen(open === sh.id ? null : sh.id)}>Sharing</button>
                    <OverflowMenu
                      label={`More for ${sh.name}`}
                      actions={[
                        {
                          label: "Rename",
                          onSelect: async () => {
                            const n = window.prompt("Shelf name", sh.name)?.trim();
                            if (!n || n === sh.name) return;
                            try {
                              await patch(`/api/shelves/${sh.id}`, { name: n });
                              reload();
                            } catch (e) {
                              toast((e as Error).message, "error");
                            }
                          },
                        },
                        {
                          label: "Delete shelf",
                          danger: true,
                          onSelect: async () => {
                            if (!window.confirm(`Delete “${sh.name}”? Books stay in the library; people it was shared with lose access to them.`)) return;
                            try {
                              await del(`/api/shelves/${sh.id}`);
                              reload();
                            } catch (e) {
                              toast((e as Error).message, "error");
                            }
                          },
                        },
                      ]}
                    />
                  </>
                )}
              </div>
              {open === sh.id && <Grants shelf={sh} onChanged={reload} />}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Grants({ shelf, onChanged }: { shelf: Shelf; onChanged: () => void }) {
  const { me, toast } = useStore();
  const [grants, setGrants] = useState<ShelfGrant[] | null>(null);
  const [users, setUsers] = useState<User[] | null>(null);
  const [pick, setPick] = useState("");
  const isOwner = me?.user.role === "owner";
  const load = useCallback(() => get<{ grants: ShelfGrant[] }>(`/api/shelves/${shelf.id}/grants`).then((d) => setGrants(d.grants)), [shelf.id]);
  useEffect(() => {
    load().catch(() => setGrants([]));
    if (isOwner) get<{ users: User[] }>("/api/admin/users").then((d) => setUsers(d.users)).catch(() => {});
  }, [load, isOwner]);
  const grant = async (userId: number, permission: "view" | "edit") => {
    try {
      await put(`/api/shelves/${shelf.id}/grants/${userId}`, { permission });
      await load();
      onChanged();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  const revoke = async (userId: number) => {
    try {
      await del(`/api/shelves/${shelf.id}/grants/${userId}`);
      await load();
      onChanged();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  const granted = new Set(grants?.map((g) => g.user.id));
  const candidates = (users ?? []).filter((u) => u.role !== "owner" && u.id !== me?.user.id && u.is_active && !granted.has(u.id));
  return (
    <div className="grants">
      {grants === null ? (
        <div className="hint">Loading…</div>
      ) : grants.length === 0 ? (
        <div className="hint">Not shared with anyone.</div>
      ) : (
        <ul>
          {grants.map((g) => (
            <li key={g.user.id}>
              <span><strong>{g.user.display_name}</strong> <span className="hint">@{g.user.username} · {g.user.role}</span></span>
              <select className="select sm" value={g.permission} aria-label={`Access for ${g.user.username}`} onChange={(e) => grant(g.user.id, e.target.value as "view" | "edit")}>
                <option value="view">Can view</option>
                <option value="edit">Can add books</option>
              </select>
              <button className="btn sm ghost" onClick={() => revoke(g.user.id)}>Remove</button>
            </li>
          ))}
        </ul>
      )}
      {isOwner ? (
        candidates.length > 0 && (
          <form className="row" onSubmit={(e) => { e.preventDefault(); if (pick) grant(Number(pick), "view").then(() => setPick("")); }}>
            <label className="sr-only" htmlFor={`grant-${shelf.id}`}>Share with</label>
            <select id={`grant-${shelf.id}`} className="select" style={{ flex: 1 }} value={pick} onChange={(e) => setPick(e.target.value)}>
              <option value="">Share with…</option>
              {candidates.map((u) => <option key={u.id} value={u.id}>{u.display_name} (@{u.username}, {u.role})</option>)}
            </select>
            <button className="btn sm" disabled={!pick}>Share</button>
          </form>
        )
      ) : (
        <p className="hint">Ask the owner to share this shelf with a reader.</p>
      )}
    </div>
  );
}
