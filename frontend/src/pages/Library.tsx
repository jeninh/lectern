import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Cover, tintStyle } from "../components/Cover";
import { EmptyState } from "../components/EmptyState";
import { MakeAudiobookRow } from "../components/MakeAudiobook";
import { ConvertPdfRow } from "../components/ConvertPdfRow";
import { OcrStatus } from "../components/OcrStatus";
import { ReadAloudFormats } from "../components/ReadAloudFormats";
import { Book, Close, Down, Download, Folder, Headphones, Pencil, Plus, Reader, Search as SearchIcon, Select, Send, Shelf as ShelfIcon, SmartFolder, Upload } from "../components/Icons";
import { FilterBar, RuleChips } from "../components/FilterBar";
import { SmartShelfDialog } from "../components/SmartShelfDialog";
import { SendToDevice } from "../components/SendToDevice";
import { DownloadButton } from "../components/Offline";
import { OverflowMenu, type MenuAction } from "../components/Menu";
import { BulkBar, type BulkTarget } from "../components/BulkBar";
import { Breadcrumbs, canNestIn, DeleteShelfDialog, dragProps, FolderCell, folderSummary, FolderPicker, ShelfNameDialog } from "../components/Folders";
import { del, get, post, put } from "../lib/api";
import { clausesOf, extractChips, formatQuery, SORTS, type Clause } from "../lib/filters";
import type { Items } from "../lib/folders";
import { childrenOf, pathLabel, shelfKey, splitKeys, workKey, worksIn, type FolderId } from "../lib/shelves";
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
import { bookFiles, formatName, formatOf, hasEpub, isPaged, PDF, readerPath } from "../lib/formats";
import type { Work } from "../lib/types";

type Sort = string;
type Status = "all" | "reading" | "unread" | "finished";
const FINISHED = 0.985;
/** Sorts the web app can do on what it has loaded; any other goes to the server. */
const LOCAL_SORTS = ["title", "author", "added"];

/** The reading status (lectern.reading_state: KOReader, the web reader, and the audio player);
 * a copy downloaded before the server reported it falls back to KOReader's percentage. */
function statusOf(w: Work): Exclude<Status, "all"> {
  if (w.reading) return w.reading.status;
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
const SORT_LABEL: Record<string, string> = { title: "Title", author: "Author", added: "Newest" };

export function LibraryPage() {
  const { works, worksLoaded, me, refreshLibrary, canUpload, canEdit, pickBooks, pickFolder, toast, imports, setReviewOpen, shelves, canCreateShelf, offlineBooks } = useStore();
  const [sendingMany, setSendingMany] = useState(false);
  const readyImports = imports.filter((i) => i.status === "ready").length;
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const sort: Sort = SORTS.some((x) => x.key === params.get("sort")) ? params.get("sort")! : "title";
  const order = (["asc", "desc"].includes(params.get("order") ?? "") ? params.get("order") : null) as "asc" | "desc" | null;
  const status = (params.get("status") as Status) || "all";
  // Filter chips (docs/filters.md): canonical syntax in `f`; the server evaluates them.
  const f = params.get("f") ?? "";
  const clauses = useMemo(() => clausesOf(f), [f]);
  const [typedError, setTypedError] = useState<string | null>(null);
  const [view, setView] = useLocalStorage<"grid" | "list">("lectern.libraryView", "grid");
  const { id } = useParams();
  const nav = useNavigate();
  const loc = useLocation();
  const input = useRef<HTMLInputElement>(null);
  // Folders (default): the root, or one shelf (?shelf=). Everything (?view=everything): flat.
  const everything = params.get("view") === "everything";
  const shelfParam = params.get("shelf");
  const folder: FolderId = everything || !shelfParam ? null : Number(shelfParam);
  const current = folder === null ? null : shelves.find((s) => s.id === folder) ?? null;
  const [suggestions, setSuggestions] = useState<MatchSuggestion[]>([]);
  const [manageShelves, setManageShelves] = useState(false);
  const [naming, setNaming] = useState<{ parent: FolderId; shelf: Shelf | null } | null>(null);
  const [deleting, setDeleting] = useState<Shelf | null>(null);
  const [sharing, setSharing] = useState<Shelf | null>(null);
  const [moving, setMoving] = useState<{ items: Items; title: string } | null>(null);
  const review = params.get("review") === "1";
  const reader = me?.user.role === "reader";

  const loadSuggestions = useCallback(async () => {
    if (!canUpload) return;
    setSuggestions((await get<{ suggestions: MatchSuggestion[] }>("/api/suggestions")).suggestions);
  }, [canUpload]);

  useEffect(() => {
    refreshLibrary().catch(() => {});
  }, [refreshLibrary]);
  useEffect(() => {
    loadSuggestions().catch(() => {});
  }, [loadSuggestions, works.length]);

  // Chips, a smart shelf, or a sort the app cannot do itself: the server lists the books.
  // With chips, a folder means everything in it and below it; at the root, the whole library.
  const chipsOn = clauses.length > 0;
  const smartHere = !!current?.smart;
  const serverMode = chipsOn || smartHere || !LOCAL_SORTS.includes(sort) || !!order;
  const offlineIds = useMemo(
    () => (clauses.some((c) => c.key === "is" && c.values.some((v) => v.a === "offline")) ? offlineBooks.map((b) => b.id) : []),
    [clauses, offlineBooks],
  );
  const serverQuery = useMemo(() => {
    if (!serverMode) return null;
    const p = new URLSearchParams({ sort });
    if (f) p.set("f", f);
    if (order) p.set("order", order);
    if (!everything) {
      if (folder !== null) {
        p.set("shelf", String(folder));
        if (chipsOn && !smartHere) p.set("subtree", "true");
      } else if (!chipsOn) p.set("unshelved", "true");
    }
    if (offlineIds.length) p.set("offline", offlineIds.join(","));
    return p.toString();
  }, [serverMode, sort, f, order, everything, folder, chipsOn, smartHere, offlineIds]);
  const [served, setServed] = useState<{ key: string; works: Work[]; error: string | null } | null>(null);
  useEffect(() => {
    if (!serverQuery) return;
    let stale = false;
    get<{ works: Work[] }>(`/api/library?${serverQuery}`)
      .then((d) => !stale && setServed({ key: serverQuery, works: d.works, error: null }))
      .catch((e) => !stale && setServed({ key: serverQuery, works: [], error: (e as Error).message }));
    return () => {
      stale = true;
    };
    // `works`: refetch whenever the library itself is refreshed (an edit, an upload).
  }, [serverQuery, works]);
  const serverLoading = serverMode && served?.key !== serverQuery;
  const here = useMemo(
    () => (serverMode ? served?.works ?? [] : everything ? works : worksIn(works, folder)),
    [serverMode, served, works, everything, folder],
  );
  const counts = useMemo(() => {
    const c: Record<Status, number> = { all: here.length, reading: 0, unread: 0, finished: 0 };
    for (const w of here) c[statusOf(w)] += 1;
    return c;
  }, [here]);
  const libCounts = useMemo(() => {
    let reading = 0;
    let finished = 0;
    for (const w of works) {
      const st = statusOf(w);
      if (st === "reading") reading += 1;
      else if (st === "finished") finished += 1;
    }
    return { reading, finished };
  }, [works]);

  const filtered = useMemo(() => {
    const needle = fold(q.trim());
    let list = needle ? here.filter((w) => fold(`${w.title} ${w.author} ${w.isbn ?? ""}`).includes(needle)) : here.slice();
    if (status !== "all") list = list.filter((w) => statusOf(w) === status);
    if (serverMode) return list; // already in the server's order
    const by: Record<string, (a: Work, b: Work) => number> = {
      title: (a, b) => a.sort_title.localeCompare(b.sort_title),
      author: (a, b) => a.sort_author.localeCompare(b.sort_author) || a.sort_title.localeCompare(b.sort_title),
      added: (a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""),
    };
    return list.sort(by[sort]);
  }, [here, q, sort, status, serverMode]);
  // Sub-shelves of this folder come first, as in a file manager; the filter matches names.
  const folders = useMemo(() => {
    if (everything || status !== "all" || chipsOn || smartHere) return [];
    const needle = fold(q.trim());
    return childrenOf(shelves, folder).filter((s) => !needle || fold(s.name).includes(needle));
  }, [everything, status, q, shelves, folder, chipsOn, smartHere]);

  const selected = id ? works.find((w) => w.id === Number(id)) ?? null : null;
  const setParam = (k: string, v: string, dflt: string) => {
    const next = new URLSearchParams(params);
    if (v && v !== dflt) next.set(k, v);
    else next.delete(k);
    setParams(next, { replace: true });
  };
  /** Open a folder: a new history entry (Back returns), keeping the sort and layout. */
  const openFolder = (to: FolderId) => {
    const next = new URLSearchParams(sort === "title" ? {} : { sort });
    if (order) next.set("order", order);
    if (to !== null) next.set("shelf", String(to));
    nav(`/app/library${next.size ? `?${next}` : ""}`);
  };
  /** Replace the chips (and keep what is left of the typed text in the search box). */
  const setFilter = (next: Clause[], rest?: string) => {
    const p = new URLSearchParams(params);
    const text = formatQuery({ clauses: next, words: [] });
    if (text) p.set("f", text);
    else p.delete("f");
    if (rest !== undefined) {
      if (rest) p.set("q", rest);
      else p.delete("q");
    }
    setParams(p, { replace: true });
  };
  /** Typed syntax becomes chips: a finished token as soon as a space follows it, all on Enter. */
  const takeTyped = (text: string, all: boolean): boolean => {
    const { clauses: more, rest, error } = extractChips(text, all);
    setTypedError(error ? error.message : null);
    if (!more.length) return false;
    setFilter([...clauses, ...more], rest);
    return true;
  };
  const [savingSmart, setSavingSmart] = useState(false);
  const [editingSmart, setEditingSmart] = useState(false);
  const setMode = (all: boolean) => {
    const next = new URLSearchParams(params);
    next.delete("shelf");
    if (all) next.set("view", "everything");
    else next.delete("view");
    nav(`/app/library${next.size ? `?${next}` : ""}`);
  };
  const open = (w: Work) => nav(`/app/library/${w.id}${loc.search}`);
  const filtering = !!q || status !== "all" || chipsOn;

  // Multi-select over folders and books together (lib/useSelection), keys "s12" and "w34"
  // (lib/shelves). The collection is windowed: only rows near the viewport are rendered, and
  // Cmd/Ctrl+A selects the whole view, its books resolved on the server.
  const [scope, setScope] = useState<HTMLDivElement | null>(null);
  const grid = view === "grid";
  const keys = useMemo(() => [...folders.map((s) => shelfKey(s.id)), ...filtered.map((w) => workKey(w.id))], [folders, filtered]);
  const { columns, width } = useGridColumns(scope, 136, 20, grid);
  const rowGap = grid ? 26 : 2;
  const win = useWindowRows(scope, { count: keys.length, columns, gap: rowGap, estimate: grid ? 280 : 58, width });
  const viewFilter = useMemo(
    () => ({
      q: q.trim(),
      status,
      shelf: everything ? null : folder,
      ...(everything || folder !== null || chipsOn ? {} : { unshelved: true }),
      ...(f ? { f } : {}),
      ...(chipsOn && !everything && folder !== null && !smartHere ? { subtree: true } : {}),
      ...(offlineIds.length ? { offline_ids: offlineIds } : {}),
    }),
    [q, status, everything, folder, f, chipsOn, smartHere, offlineIds],
  );
  // The view's books as the server counted them when everything was selected: how many and
  // which. Kept as the user saw them ("All N selected"); the server refuses the action if the
  // view no longer holds exactly those books.
  const [viewInfo, setViewInfo] = useState<{ total: number; view_token: string } | null>(null);
  const bookTotal = viewInfo?.total ?? filtered.length;
  const s = useSelection<string>({
    scope,
    keys,
    columns,
    total: folders.length + bookTotal,
    onOpen: (key) => (key[0] === "s" ? openFolder(Number(key.slice(1))) : nav(`/app/library/${key.slice(1)}${loc.search}`)),
    reveal: win.reveal,
    rendered: [win.start * columns, Math.min(keys.length, win.end * columns)],
    resetOn: JSON.stringify(viewFilter),
    global: true,
    marquee: true,
  });
  useEffect(() => {
    if (!s.sel.all) return setViewInfo(null);
    const p = new URLSearchParams({ q: viewFilter.q, status: viewFilter.status });
    if (viewFilter.shelf !== null) p.set("shelf", String(viewFilter.shelf));
    if ("unshelved" in viewFilter) p.set("unshelved", "true");
    if (viewFilter.f) p.set("f", viewFilter.f);
    if (viewFilter.subtree) p.set("subtree", "true");
    if (viewFilter.offline_ids) p.set("offline", viewFilter.offline_ids.join(","));
    let stale = false;
    get<{ total: number; view_token: string }>(`/api/library/count?${p}`).then((d) => !stale && setViewInfo(d)).catch((e) => toast((e as Error).message, "error"));
    return () => {
      stale = true;
    };
  }, [s.sel.all, viewFilter, toast]);
  const picked = splitKeys(s.sel.all ? [...s.sel.set] : s.selectedKeys());
  const pickedShelves = s.sel.all ? folders.map((f) => f.id).filter((x) => !picked.shelves.includes(x)) : picked.shelves;
  const pickedBooks = s.sel.all ? Math.max(0, bookTotal - picked.works.length) : picked.works.length;
  /** The selected books: explicit ids, or for "all" the view the user saw minus what they
   * deselected; null while that view is still being counted. */
  const target = (): BulkTarget | null => {
    if (!s.sel.all) return { work_ids: picked.works };
    if (!viewInfo) return null;
    return { selection: { filter: viewFilter, exclude_ids: picked.works, view_token: viewInfo.view_token, expected_count: pickedBooks } };
  };
  const from: FolderId | "all" = everything ? "all" : folder;
  const selectionItems = (): Items => ({ shelves: pickedShelves, works: pickedBooks ? target() ?? { work_ids: [] } : null, workCount: pickedBooks, from });
  const describe = (shelvesN: number, books: number) => [shelvesN && plural(shelvesN, "shelf", "shelves"), books && plural(books, "book")].filter(Boolean).join(" and ");
  /** Dragging a selected item drags the selection; an unselected one, just itself. */
  const dragFor = (key: string) =>
    dragProps(
      !reader && !s.armed,
      () => (s.isSelected(key) ? selectionItems() : key[0] === "s" ? { shelves: [Number(key.slice(1))], works: null, workCount: 0, from } : { shelves: [], works: { work_ids: [Number(key.slice(1))] }, workCount: 1, from }),
      () => (s.isSelected(key) ? describe(pickedShelves.length, pickedBooks) : key[0] === "s" ? shelves.find((x) => shelfKey(x.id) === key)?.name ?? "" : works.find((w) => workKey(w.id) === key)?.title ?? ""),
    );
  const pickedTitles = useMemo(() => filtered.filter((w) => s.isSelected(workKey(w.id))).map((w) => w.title), [filtered, s]);
  const rowsShown = Array.from({ length: win.end - win.start }, (_, i) => win.start + i);
  const canCreateHere = !reader && (folder === null ? canCreateShelf : !!current && current.permission !== "view" && !current.smart) && canNestIn(shelves, folder);
  const folderMenu = (sh: Shelf): MenuAction[] => [
    { label: "Open", onSelect: () => openFolder(sh.id) },
    ...(sh.permission === "owner"
      ? [
          { label: "Rename…", onSelect: () => setNaming({ parent: sh.parent_id, shelf: sh }) },
          { label: "Move to…", onSelect: () => setMoving({ items: { shelves: [sh.id], works: null, workCount: 0, from: folder }, title: `Move “${sh.name}”` }) },
          { label: "Sharing…", onSelect: () => setSharing(sh) },
          { label: "Delete…", danger: true, onSelect: () => setDeleting(sh) },
        ]
      : []),
  ];
  const coversOf = (sh: Shelf) => worksIn(works, sh.id).slice(0, 3);
  const unknownFolder = !everything && folder !== null && worksLoaded && !current;
  const emptyHere = keys.length === 0;

  return (
    <>
      <PageTitle
        sub={worksLoaded ? `${works.length} ${works.length === 1 ? "book" : "books"} · ${libCounts.reading} reading · ${libCounts.finished} finished` : undefined}
        aside={
          <>
            {(canCreateShelf || shelves.length > 0) && <button className="btn ghost" onClick={() => setManageShelves(true)}><ShelfIcon /> Shelves</button>}
            {keys.length > 0 && (
              <button className="btn ghost" aria-pressed={s.selecting} onClick={() => (s.selecting ? s.exit() : s.setSelecting(true))} title={`Click a book's circle to select it (or ${MOD}-click); while selecting, clicks add and remove. Shift-click for a range, ${MOD} A for all`}>
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
                    { label: "Choose files (EPUB, PDF, CBZ, MOBI, M4B, MP3)…", onSelect: pickBooks },
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
      <div className="folder-bar">
        {everything ? <h2 className="crumbs-title">Everything <span className="hint">every book, on any shelf or none</span></h2> : <Breadcrumbs folder={folder} />}
        <span className="spacer" />
        {!everything && canCreateHere && (
          <button className="btn sm ghost" onClick={() => setNaming({ parent: folder, shelf: null })}><Folder /> New shelf</button>
        )}
        {!everything && current?.permission === "owner" && <OverflowMenu label={`More for ${current.name}`} actions={folderMenu(current).slice(1)} />}
        <div className="seg seg-text" role="group" aria-label="Library view">
          <button aria-pressed={!everything} onClick={() => setMode(false)} title="Shelves as folders"><Folder /> Folders</button>
          <button aria-pressed={everything} onClick={() => setMode(true)} title="Every book, flat"><Book /> Everything</button>
        </div>
      </div>
      <div className="filter-bar" role="search" aria-label="Library">
        <div className="filter-input">
          <SearchIcon aria-hidden="true" />
          <label className="sr-only" htmlFor="lib-q">Filter by title, author, or ISBN</label>
          <input
            id="lib-q"
            ref={input}
            data-page-filter
            className="input"
            placeholder={everything || folder === null ? "Filter by title or author" : `Filter ${current?.name ?? "this shelf"}`}
            value={q}
            onChange={(e) => {
              if (!takeTyped(e.target.value, false)) setParam("q", e.target.value, "");
            }}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.stopPropagation();
                setTypedError(null);
                if (q) setParam("q", "", "");
                else input.current?.blur();
              } else if (e.key === "Enter" && takeTyped(q, true)) {
                e.preventDefault();
              } else if (e.key === "Backspace" && !q && clauses.length && input.current?.selectionStart === 0) {
                // Backspace in an empty box takes the last chip back into it, for editing.
                e.preventDefault();
                setFilter(clauses.slice(0, -1), formatQuery({ clauses: clauses.slice(-1), words: [] }));
              } else if (e.key === "Enter" && filtered[0]) {
                // Without this the Enter keypress lands on the sheet's freshly focused Close button.
                e.preventDefault();
                open(filtered[0]);
              }
            }}
            aria-describedby={typedError ? "lib-q-error" : undefined}
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
          {(Object.keys(STATUS_LABEL) as Status[]).map((st) => (
            <button key={st} className="chip" aria-pressed={status === st} onClick={() => setParam("status", st, "all")}>
              {STATUS_LABEL[st]} <span className="n">{counts[st]}</span>
            </button>
          ))}
        </div>
        <div className="chips" role="group" aria-label="Sort by">
          {Object.keys(SORT_LABEL).map((so) => (
            <button key={so} className="chip subtle" aria-pressed={sort === so} onClick={() => setParam("sort", so, "title")}>
              {SORT_LABEL[so]}
            </button>
          ))}
          {!LOCAL_SORTS.includes(sort) && (
            <button className="chip subtle" aria-pressed="true" onClick={() => setParam("sort", "title", "title")}>
              {SORTS.find((x) => x.key === sort)?.label}
            </button>
          )}
          <OverflowMenu
            label="More ways to sort"
            className="chip subtle more-sorts"
            trigger={<>More <Down /></>}
            actions={[
              ...SORTS.filter((x) => !LOCAL_SORTS.includes(x.key)).map((x) => ({ label: x.label, checked: sort === x.key, onSelect: () => setParam("sort", x.key, "title") })),
              {
                label: (order ?? (SORTS.find((x) => x.key === sort)?.desc ? "desc" : "asc")) === "desc" ? "Reverse: smallest or oldest first" : "Reverse: largest or newest first",
                onSelect: () => {
                  const dflt = SORTS.find((x) => x.key === sort)?.desc ? "desc" : "asc";
                  const now = order ?? dflt;
                  setParam("order", now === "desc" ? "asc" : "desc", dflt);
                },
              },
            ]}
          />
        </div>
        <div className="seg" role="group" aria-label="Layout">
          <button aria-pressed={view === "grid"} onClick={() => setView("grid")} aria-label="Grid" title="Grid"><GridIcon /></button>
          <button aria-pressed={view === "list"} onClick={() => setView("list")} aria-label="List" title="List"><ListIcon /></button>
        </div>
        <span className="sr-only" aria-live="polite">{filtering ? (chipsOn ? `${filtered.length} ${filtered.length === 1 ? "book" : "books"} shown` : `${filtered.length} of ${here.length} books shown`) : ""}</span>
      </div>
      {typedError && <p id="lib-q-error" className="field-error typed-error" role="status">{typedError}</p>}
      {smartHere && current && (
        <div className="smart-rule">
          <SmartFolder aria-hidden="true" />
          <span className="hint">Smart shelf: books where</span>
          {current.filter !== undefined ? <RuleChips clauses={clausesOf(current.filter)} /> : <span className="hint">its owner’s filter matches, among the books you can see.</span>}
          {current.permission === "owner" && <button className="btn sm ghost" onClick={() => setEditingSmart(true)}><Pencil /> Edit filter</button>}
        </div>
      )}
      <FilterBar
        clauses={clauses}
        onChange={(next) => setFilter(next)}
        error={served?.key === serverQuery ? served?.error : null}
        actions={
          chipsOn && !reader && canCreateShelf ? (
            <button className="btn sm ghost" onClick={() => setSavingSmart(true)}><SmartFolder /> Save as smart shelf…</button>
          ) : undefined
        }
      />

      {!worksLoaded || (serverLoading && !served) ? (
        <div className="book-grid" aria-busy="true" aria-label="Loading">{Array.from({ length: 6 }, (_, i) => <div key={i} className="book skeleton"><div className="cover" /></div>)}</div>
      ) : unknownFolder ? (
        <EmptyState icon={<ShelfIcon />} title="This shelf isn’t available" action={<button className="btn" onClick={() => openFolder(null)}>Back to the Library</button>}>
          It was deleted, or it isn’t shared with you.
        </EmptyState>
      ) : works.length === 0 && shelves.length === 0 ? (
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
      ) : emptyHere ? (
        filtering ? (
          <EmptyState
            icon={<SearchIcon />}
            title={q ? `Nothing matches “${q}”` : chipsOn ? "No books match these filters" : `No ${STATUS_LABEL[status].toLowerCase()} books here`}
            action={<button className="btn" onClick={() => setParams(new URLSearchParams([...params].filter(([k]) => k === "shelf" || k === "view" || k === "sort" || k === "order")), { replace: true })}>Clear filters</button>}
          >
            {q ? "Try a shorter word, an author’s surname, or another status." : chipsOn ? "Remove a filter, or switch one to “is not”." : "Books move here as you read on KOReader, the web reader, or the player."}
          </EmptyState>
        ) : (
          smartHere && current ? (
            <EmptyState icon={<SmartFolder />} title={`Nothing matches “${current.name}” yet`}>
              A smart shelf fills itself: books show up here as soon as they match its filter.
            </EmptyState>
          ) : <EmptyState icon={<Folder />} title={current ? `“${current.name}” is empty` : "Every book is on a shelf"} action={canCreateHere ? <button className="btn" onClick={() => setNaming({ parent: folder, shelf: null })}><Folder /> New shelf</button> : undefined}>
            {current ? "Drag books or shelves here, or use Move to… on a selection." : "Books on no shelf show up here. Open a shelf, or switch to Everything."}
          </EmptyState>
        )
      ) : (
        <div
          ref={setScope}
          className={grid ? "book-grid" : "book-list"}
          role="grid"
          aria-label={chipsOn ? "Books matching the filters" : everything ? (q ? `Books matching ${q}` : "Every book") : current ? `${current.smart ? "Books in the smart shelf" : "Shelves and books in"} ${current.name}` : "Shelves and books not on a shelf"}
          aria-multiselectable="true"
          aria-rowcount={win.rows}
          aria-colcount={columns}
          data-has-selection={s.selecting || undefined}
          style={{ paddingTop: win.padTop, paddingBottom: win.padBottom }}
          {...s.scopeProps}
        >
          {rowsShown.map((r) => (
            <div key={r} role="row" aria-rowindex={r + 1} data-vrow className="vrow" style={{ gridTemplateColumns: grid ? `repeat(${columns}, minmax(0, 1fr))` : undefined, height: win.stride - rowGap, marginBottom: rowGap }}>
              {keys.slice(r * columns, (r + 1) * columns).map((key, c) => {
                if (key[0] === "s") {
                  const sh = folders.find((x) => shelfKey(x.id) === key)!;
                  return <FolderCell key={key} shelf={sh} grid={grid} cell={s.cellProps(key)} col={c + 1} covers={coversOf(sh)} menu={reader ? [] : folderMenu(sh)} drag={dragFor(key)} />;
                }
                const w = filtered[r * columns + c - folders.length];
                return <BookCell key={key} work={w} q={q} grid={grid} cell={s.cellProps(key)} col={c + 1} drag={dragFor(key)} />;
              })}
            </div>
          ))}
          {s.marquee && <div className="marquee" style={s.marquee} aria-hidden="true" />}
        </div>
      )}

      <BulkBar
          open={s.selecting}
          ready={!s.sel.all || !!viewInfo}
          count={s.count}
          books={pickedBooks}
          all={s.allSelected}
          total={folders.length + bookTotal}
          target={target}
          titles={pickedTitles}
          shelves={shelves.filter((sh) => sh.permission !== "view" && !sh.smart)}
          onSelectAll={s.selectAll}
          onClear={s.exit}
          onDone={async () => {
            await refreshLibrary().catch(() => {});
          }}
          canEdit={canEdit}
          canAlign={canEdit}
          onSend={() => setSendingMany(true)}
          onMove={reader ? undefined : () => setMoving({ items: selectionItems(), title: `Move or add ${describe(pickedShelves.length, pickedBooks)}` })}
        />
      <SendToDevice
        open={sendingMany}
        target={() => target() ?? { work_ids: [] }}
        count={pickedBooks}
        title={`Send ${plural(pickedBooks, "book")} to a device`}
        onClose={() => setSendingMany(false)}
        onSent={s.exit}
      />
      <SmartShelfDialog
        open={savingSmart}
        filter={formatQuery({
          // Everything this view filters by: the chips, the status chip, and the typed words.
          clauses: [...clauses, ...(status !== "all" ? clausesOf(`status:${status}`) : [])],
          words: q.trim() ? [{ text: q.trim(), negate: false }] : [],
        })}
        sort={sort}
        order={order}
        parent={!everything && folder !== null && current && current.permission !== "view" && !current.smart ? folder : null}
        onClose={() => setSavingSmart(false)}
        onSaved={(sh) => {
          setSavingSmart(false);
          toast(`Saved “${sh.name}”. It keeps itself up to date.`);
          refreshLibrary().then(() => openFolder(sh.id)).catch(() => {});
        }}
      />
      <SmartShelfDialog
        open={editingSmart}
        shelf={current}
        onClose={() => setEditingSmart(false)}
        onSaved={() => {
          setEditingSmart(false);
          refreshLibrary().catch(() => {});
        }}
      />
      <FolderPicker items={moving?.items ?? null} title={moving?.title ?? ""} onClose={() => { setMoving(null); }} />
      <ShelfNameDialog open={!!naming} parent={naming?.parent} shelf={naming?.shelf} onClose={() => setNaming(null)} />
      <DeleteShelfDialog shelf={deleting} onClose={() => setDeleting(null)} onDeleted={(parent) => deleting?.id === folder && openFolder(parent)} />
      <Sheet open={!!sharing} onClose={() => setSharing(null)} title={sharing ? `Share “${pathLabel(sharing)}”` : ""} labelledBy="share-title">
        {sharing && (
          <div className="stack">
            <p className="sub" style={{ margin: 0 }}>Sharing a shelf shares everything inside it, sub-shelves included. People see it as a top-level shelf; shelves above it stay private.</p>
            <Grants shelf={sharing} onChanged={() => refreshLibrary().catch(() => {})} />
          </div>
        )}
      </Sheet>
      <Sheet open={!!selected} onClose={() => nav(`/app/library${loc.search}`)} title={selected?.title ?? ""}>
        {selected && <WorkDetail work={selected} shelves={shelves} onShelvesChanged={() => refreshLibrary().catch(() => {})} />}
      </Sheet>
      <Sheet open={manageShelves} onClose={() => setManageShelves(false)} title="Shelves" labelledBy="shelves-title">
        <ShelvesManager shelves={shelves} canCreate={canCreateHere} folder={folder} reload={() => refreshLibrary().catch(() => {})} onOpen={(sid) => { setManageShelves(false); openFolder(sid); }} />
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

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** One book in the Library grid or list: a grid cell (lib/useSelection). A click or tap opens
 * it; its circle, a long press, or a modifier click selects it, and while anything is selected
 * clicks toggle and Enter opens. */
function BookCell({ work: w, q, grid, cell, col, drag }: { work: Work; q: string; grid: boolean; cell: CellProps; col: number; drag: ReturnType<typeof dragProps> }) {
  const label = `${w.title} by ${w.author}${w.progress ? `, ${percent(w.progress.percentage)} read` : ""}`;
  const pick = (
    <span className="pick" data-sel-toggle aria-hidden="true" title="Select">
      {cell["aria-selected"] ? "✓" : ""}
    </span>
  );
  return grid ? (
    <div className="book" style={tintStyle(w)} aria-label={label} aria-colindex={col} {...cell} {...drag}>
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
    <div className="book" style={tintStyle(w)} aria-label={label} aria-colindex={col} {...cell} {...drag}>
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
  const member = new Set(work.shelf_ids ?? []);
  const [sending, setSending] = useState(false);
  const [merging, setMerging] = useState(false);

  const toggleShelf = async (sh: Shelf) => {
    const on = member.has(sh.id);
    try {
      if (on) await del(`/api/shelves/${sh.id}/items/${work.id}`);
      else await post(`/api/shelves/${sh.id}/items`, { work_id: work.id });
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
            {(ebook || work.audio) && <DownloadButton work={work} />}
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
                  {formatOf(f) === "PDF" && <OcrStatus file={f} title={work.title} />}
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
          {(epub || work.files.some((f) => f.media_type === PDF)) && !work.audio && canUpload && <MakeAudiobookRow work={work} />}
          {work.files.some((f) => f.media_type === PDF) && canUpload && <ConvertPdfRow work={work} />}
          <ReadAloudFormats work={work} active />
        </ul>
      </div>
      {editable.length > 0 && (
        <div>
          <h3 className="detail-h">Shelves</h3>
          <div className="chips" role="group" aria-label="Shelves for this book">
            {editable.map((sh) => (
              <button key={sh.id} className="chip subtle" aria-pressed={member.has(sh.id)} onClick={() => toggleShelf(sh)} title={pathLabel(sh)}>
                {member.has(sh.id) ? "✓ " : "+ "}{pathLabel(sh)}
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

/** Every shelf as an indented tree: open, share, rename, delete, and make one in the folder
 * the Library is showing. */
function ShelvesManager({ shelves, canCreate, folder, reload, onOpen }: { shelves: Shelf[]; canCreate: boolean; folder: FolderId; reload: () => void; onOpen: (id: number) => void }) {
  const { me } = useStore();
  const [open, setOpen] = useState<number | null>(null);
  const [naming, setNaming] = useState<{ parent: FolderId; shelf: Shelf | null } | null>(null);
  const [deleting, setDeleting] = useState<Shelf | null>(null);
  const here = folder === null ? null : shelves.find((s) => s.id === folder) ?? null;
  return (
    <div className="stack">
      <p className="sub" style={{ margin: 0 }}>
        {me?.user.role === "reader"
          ? "Shelves the household shares with you, and the shelves inside them. You see only the books on them."
          : "Shelves are folders: they nest, and a book can be on several. Share a shelf with readers in your household and they see it and everything inside it."}
      </p>
      {canCreate && (
        <div className="row">
          <button className="btn" onClick={() => setNaming({ parent: folder, shelf: null })}><Plus /> New shelf {here ? `in ${here.name}` : "at the top level"}</button>
        </div>
      )}
      {shelves.length === 0 ? (
        <p className="hint">No shelves yet.</p>
      ) : (
        <ul className="shelf-list">
          {shelves.map((sh) => (
            <li key={sh.id} style={{ marginLeft: (sh.path.length - 1) * 18 }}>
              <div className="shelf-row">
                <Folder />
                <div>
                  <button className="link-btn" onClick={() => onOpen(sh.id)}><strong>{sh.name}</strong></button>
                  <div className="hint">
                    {folderSummary(sh)}
                    {sh.permission === "owner" ? (sh.grant_count ? ` · shared with ${sh.grant_count}` : "") : ` · ${sh.owner ? `@${sh.owner}’s` : "shared"} · you can ${sh.permission}`}
                  </div>
                </div>
                {sh.permission === "owner" && (
                  <>
                    <button className="btn sm ghost" aria-expanded={open === sh.id} onClick={() => setOpen(open === sh.id ? null : sh.id)}>Sharing</button>
                    <OverflowMenu
                      label={`More for ${sh.name}`}
                      actions={[
                        { label: "Rename…", onSelect: () => setNaming({ parent: sh.parent_id, shelf: sh }) },
                        { label: "Delete…", danger: true, onSelect: () => setDeleting(sh) },
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
      <ShelfNameDialog open={!!naming} parent={naming?.parent} shelf={naming?.shelf} onClose={() => setNaming(null)} />
      <DeleteShelfDialog shelf={deleting} onClose={() => setDeleting(null)} />
    </div>
  );
}

function Grants({ shelf, onChanged }: { shelf: Shelf; onChanged: () => void }) {
  const { me, toast } = useStore();
  const [grants, setGrants] = useState<ShelfGrant[] | null>(null);
  const [inherited, setInherited] = useState<ShelfGrant[]>([]);
  const [users, setUsers] = useState<User[] | null>(null);
  const [pick, setPick] = useState("");
  const isOwner = me?.user.role === "owner";
  const load = useCallback(
    () =>
      get<{ grants: ShelfGrant[]; inherited: ShelfGrant[] }>(`/api/shelves/${shelf.id}/grants`).then((d) => {
        setGrants(d.grants);
        setInherited(d.inherited ?? []);
      }),
    [shelf.id],
  );
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
      {inherited.length > 0 && (
        <ul className="inherited" aria-label="Shared through shelves above">
          {inherited.map((g) => (
            <li key={`${g.shelf?.id}-${g.user.id}`}>
              <span><strong>{g.user.display_name}</strong> <span className="hint">@{g.user.username} · can {g.permission === "edit" ? "add books" : "view"} through “{g.shelf?.name}”</span></span>
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
