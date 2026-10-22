// The Library's folders: shelf tiles, breadcrumbs, the sidebar tree, and the dialogs to make,
// rename, move, and delete shelves (lib/shelves has the rules, lib/folders the moves).

import { useEffect, useMemo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { patch, post } from "../lib/api";
import { deleteShelf, startDrag, endDrag, useDropTarget, useFolderOps, type Items } from "../lib/folders";
import { useLocalStorage } from "../lib/hooks";
import { ancestry, byId, childrenOf, MAX_DEPTH, pathLabel, type FolderId } from "../lib/shelves";
import { useStore } from "../lib/store";
import type { Shelf, Work } from "../lib/types";
import type { CellProps } from "../lib/useSelection";
import { Cover } from "./Cover";
import { Chevron, Folder, Library as LibraryIcon } from "./Icons";
import { OverflowMenu, type MenuAction } from "./Menu";
import { Sheet } from "./Sheet";

export const folderUrl = (id: FolderId) => (id === null ? "/app/library" : `/app/library?shelf=${id}`);

const plural = (k: number, one: string, many = `${one}s`) => `${k} ${k === 1 ? one : many}`;

/** "3 books · 2 shelves", or "Empty". */
export function folderSummary(s: Pick<Shelf, "work_count" | "shelf_count">): string {
  const parts = [s.work_count ? plural(s.work_count, "book") : "", s.shelf_count ? plural(s.shelf_count, "shelf", "shelves") : ""].filter(Boolean);
  return parts.join(" · ") || "Empty";
}

/** A shelf in the Library grid or list: opened like a folder (double-click, Enter, or a tap),
 * selected like a book, dragged and dropped onto. */
export function FolderCell({ shelf, grid, cell, col, covers, menu, drag }: { shelf: Shelf; grid: boolean; cell: CellProps; col: number; covers: Work[]; menu: MenuAction[]; drag: { draggable: boolean; onDragStart: (e: React.DragEvent) => void; onDragEnd: () => void } }) {
  const drop = useDropTarget()(shelf.id);
  const label = `Shelf ${shelf.name}, ${folderSummary(shelf)}`;
  const pick = (
    <span className="pick" data-sel-toggle aria-hidden="true" title="Select">
      {cell["aria-selected"] ? "✓" : ""}
    </span>
  );
  const more = menu.length > 0 && (
    <span className="folder-more">
      <OverflowMenu label={`More for ${shelf.name}`} actions={menu} />
    </span>
  );
  return grid ? (
    <div className="book folder" aria-label={label} aria-colindex={col} {...cell} {...drag} {...drop}>
      {pick}
      <span className="cover-wrap">
        <span className="cover folder-cover">
          <span className="fan" aria-hidden="true">
            {covers.slice(0, 3).map((w) => (
              <Cover key={w.id} work={w} />
            ))}
          </span>
          <Folder className="folder-icon" aria-hidden="true" />
        </span>
        {more}
      </span>
      <strong>{shelf.name}</strong>
      <span>{folderSummary(shelf)}{shelf.permission === "view" ? " · view only" : ""}</span>
    </div>
  ) : (
    <div className="book folder" aria-label={label} aria-colindex={col} {...cell} {...drag} {...drop}>
      {pick}
      <span className="folder-row-icon" aria-hidden="true"><Folder /></span>
      <span className="titles">
        <strong>{shelf.name}</strong>
        <span>{folderSummary(shelf)}</span>
      </span>
      <span className="when">{shelf.permission === "owner" ? (shelf.grant_count ? `shared with ${shelf.grant_count}` : "") : `you can ${shelf.permission}`}</span>
      {more || <span />}
    </div>
  );
}

/** Library › Kids › Picture books: each crumb opens that folder and takes drops. */
export function Breadcrumbs({ folder }: { folder: FolderId }) {
  const { shelves } = useStore();
  const drop = useDropTarget();
  const chain = folder === null ? [] : ancestry(shelves, folder);
  return (
    <nav className="crumbs" aria-label="Folder">
      <ol>
        <li>
          <Link to={folderUrl(null)} className="crumb" aria-current={folder === null ? "page" : undefined} {...drop(null)}>
            <LibraryIcon aria-hidden="true" /> Library
          </Link>
        </li>
        {chain.map((s, i) => (
          <li key={s.id}>
            <span className="sep" aria-hidden="true">›</span>
            <Link to={folderUrl(s.id)} className="crumb" aria-current={i === chain.length - 1 ? "page" : undefined} {...drop(s.id)}>
              {s.name}
            </Link>
          </li>
        ))}
      </ol>
    </nav>
  );
}

/** Drag props for one grid item: dragging a selected item drags the whole selection. */
export function dragProps(enabled: boolean, items: () => Items, label: () => string) {
  return {
    draggable: enabled,
    onDragStart: (e: React.DragEvent) => {
      if (!enabled) return;
      startDrag(e, items(), label());
    },
    onDragEnd: endDrag,
  };
}

/** The sidebar's collapsible shelf tree, under Library. Rows open folders and take drops. */
export function ShelfTree() {
  const { shelves } = useStore();
  const loc = useLocation();
  const [open, setOpen] = useLocalStorage<number[]>("lectern.shelfTreeOpen", []);
  const [shown, setShown] = useLocalStorage("lectern.shelfTreeShown", true);
  const drop = useDropTarget();
  const params = new URLSearchParams(loc.search);
  const active = loc.pathname.startsWith("/app/library") && params.get("view") !== "everything" ? Number(params.get("shelf") ?? 0) || null : undefined;
  // Keep the open folder's ancestors expanded, so the active row is visible.
  useEffect(() => {
    if (!active) return;
    const up = ancestry(shelves, active).slice(0, -1).map((s) => s.id);
    if (up.some((id) => !open.includes(id))) setOpen([...new Set([...open, ...up])]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, shelves]);
  if (!shelves.length) return null;
  const toggle = (id: number) => setOpen(open.includes(id) ? open.filter((x) => x !== id) : [...open, id]);
  const rows = (parent: number | null, depth: number): JSX.Element[] =>
    childrenOf(shelves, parent).map((s) => {
      const kids = s.shelf_count > 0;
      const expanded = open.includes(s.id);
      return (
        <li key={s.id} role="treeitem" aria-expanded={kids ? expanded : undefined} aria-selected={active === s.id} aria-level={depth + 1}>
          <div className="tree-row" style={{ paddingLeft: 6 + depth * 14 }} {...drop(s.id)}>
            {kids ? (
              <button className="twisty" aria-label={`${expanded ? "Collapse" : "Expand"} ${s.name}`} onClick={() => toggle(s.id)} tabIndex={-1}>
                <Chevron data-open={expanded || undefined} />
              </button>
            ) : (
              <span className="twisty" aria-hidden="true" />
            )}
            <Link to={folderUrl(s.id)} className="tree-link" aria-current={active === s.id ? "page" : undefined} title={pathLabel(s)}>
              <Folder aria-hidden="true" />
              <span>{s.name}</span>
            </Link>
          </div>
          {kids && expanded && (
            <ul role="group">{rows(s.id, depth + 1)}</ul>
          )}
        </li>
      );
    });
  return (
    <div className="shelf-tree">
      <button className="tree-head" aria-expanded={shown} onClick={() => setShown(!shown)}>
        <Chevron data-open={shown || undefined} aria-hidden="true" /> Shelves
      </button>
      {shown && (
        <ul role="tree" aria-label="Shelves">
          {rows(null, 0)}
        </ul>
      )}
    </div>
  );
}

/** Make a shelf in `parent`, or rename `shelf`. */
export function ShelfNameDialog({ open, parent, shelf, onClose, onSaved }: { open: boolean; parent?: FolderId; shelf?: Shelf | null; onClose: () => void; onSaved?: (s: Shelf) => void }) {
  const { shelves, toast, refreshLibrary } = useStore();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => setName(shelf?.name ?? ""), [open, shelf]);
  const where = parent == null ? "the top level" : byId(shelves).get(parent)?.name ?? "this shelf";
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const s = shelf ? await patch<Shelf>(`/api/shelves/${shelf.id}`, { name: name.trim() }) : await post<Shelf>("/api/shelves", { name: name.trim(), parent_id: parent ?? null });
      await refreshLibrary().catch(() => {});
      onClose();
      onSaved?.(s);
    } catch (ex) {
      toast((ex as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Sheet open={open} onClose={onClose} title={shelf ? `Rename “${shelf.name}”` : `New shelf in ${where}`} variant="dialog" labelledBy="shelf-name-title">
      <form className="stack" onSubmit={save}>
        <label className="field">
          <span>Name</span>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} maxLength={128} required pattern="[^/]+" title="A shelf name cannot contain /" data-autofocus placeholder="e.g. Picture books" />
        </label>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || !name.trim() || name.trim() === shelf?.name}>{shelf ? "Rename" : "Create"}</button>
        </div>
      </form>
    </Sheet>
  );
}

/** Pick where items go: the top level or a shelf, as a tree. Move takes them out of the
 * folder they are in; Add (books only) keeps them there too. */
export function FolderPicker({ items, onClose, title }: { items: Items | null; onClose: () => void; title: string }) {
  const { shelves } = useStore();
  const { blocked, place } = useFolderOps();
  const [to, setTo] = useState<FolderId | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  useEffect(() => setTo(undefined), [items]);
  const rows = useMemo(() => [null, ...shelves.map((s) => s.id)] as FolderId[], [shelves]);
  if (!items) return null;
  const m = byId(shelves);
  const depth = (id: FolderId) => (id === null ? 0 : ancestry(shelves, id).length);
  const go = async (mode: "move" | "add") => {
    if (to === undefined) return;
    setBusy(true);
    const ok = await place(items, to, mode);
    setBusy(false);
    if (ok) onClose();
  };
  const moveWhy = to === undefined ? "Choose where" : blocked(items, to, "move");
  const addWhy = to === undefined ? "Choose where" : blocked(items, to, "add");
  return (
    <Sheet open onClose={onClose} title={title} variant="dialog" labelledBy="folder-picker-title">
      <div className="stack">
        <div className="folder-pick" role="listbox" aria-label="Destination">
          {rows.map((id) => {
            const s = id === null ? null : m.get(id)!;
            const why = blocked(items, id, "move") && blocked(items, id, "add");
            return (
              <button
                key={id ?? "root"}
                type="button"
                role="option"
                aria-selected={to === id}
                aria-disabled={!!why || undefined}
                title={why ?? undefined}
                className="folder-pick-row"
                style={{ paddingLeft: 10 + depth(id) * 16 }}
                onClick={() => !why && setTo(id)}
              >
                {s ? <Folder aria-hidden="true" /> : <LibraryIcon aria-hidden="true" />}
                <span>{s ? s.name : "Library (top level, on no shelf)"}</span>
                {s && <span className="hint">{folderSummary(s)}</span>}
              </button>
            );
          })}
        </div>
        <p className="hint" style={{ margin: 0 }}>
          Move takes {items.shelves.length ? "them" : "the books"} out of {items.from === null ? "the top level" : items.from === "all" ? "no shelf" : `“${m.get(items.from)?.name ?? "this shelf"}”`}
          {items.from === "all" ? " (from Everything, Move to the top level takes books off every shelf you can change)" : ""}. Add keeps books where they are as well; a book can be on several shelves. Tip: drag onto a shelf, holding Alt (Option on a Mac) to add.
        </p>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          {!items.shelves.length && (
            <button className="btn" disabled={busy || !!addWhy} title={addWhy ?? undefined} onClick={() => go("add")}>Add to shelf</button>
          )}
          <button className="btn primary" disabled={busy || !!moveWhy} title={moveWhy ?? undefined} onClick={() => go("move")}>Move here</button>
        </div>
      </div>
    </Sheet>
  );
}

/** Delete a shelf. An empty one just goes; otherwise choose: its sub-shelves and books move
 * up a level, or its books (and everything below) go to the Trash with the shelves. */
export function DeleteShelfDialog({ shelf, onClose, onDeleted }: { shelf: Shelf | null; onClose: () => void; onDeleted?: (parent: FolderId) => void }) {
  const { shelves, works, toast, refreshLibrary, canEdit } = useStore();
  const [choice, setChoice] = useState<"move_up" | "trash">("move_up");
  const [busy, setBusy] = useState(false);
  useEffect(() => setChoice("move_up"), [shelf]);
  if (!shelf) return null;
  const below = shelves.filter((s) => s.id !== shelf.id && ancestry(shelves, s.id).some((a) => a.id === shelf.id));
  const inTree = new Set([shelf.id, ...below.map((s) => s.id)]);
  const books = works.filter((w) => w.shelf_ids?.some((id) => inTree.has(id))).length;
  const empty = !shelf.work_count && !shelf.shelf_count;
  const parent = shelf.parent_id === null ? null : byId(shelves).get(shelf.parent_id) ?? null;
  const up = parent ? `“${parent.name}”` : "the top level";
  const run = async () => {
    setBusy(true);
    try {
      const r = await deleteShelf(shelf.id, empty ? null : choice);
      if (r && !r.deleted) toast("Some books could not be moved to the Trash, so the shelf was kept.", "error");
      else {
        const batch = r?.batch_id;
        toast(
          `Deleted “${shelf.name}”${!empty && choice === "trash" ? ` and moved ${plural(books, "book")} to the Trash` : ""}.`,
          "info",
          batch
            ? {
                label: "Undo",
                run: async () => {
                  try {
                    await post(`/api/works/bulk/${batch}/undo`);
                    toast("Books restored (at the top level; the shelf stays deleted).");
                  } catch (e) {
                    toast((e as Error).message, "error");
                  }
                  await refreshLibrary().catch(() => {});
                },
              }
            : undefined,
        );
        onDeleted?.(shelf.parent_id);
      }
      await refreshLibrary().catch(() => {});
      onClose();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Sheet open onClose={onClose} title={`Delete “${shelf.name}”?`} variant="dialog" labelledBy="delete-shelf-title">
      <div className="stack">
        {empty ? (
          <p className="sub" style={{ margin: 0 }}>It’s empty. People it was shared with lose nothing.</p>
        ) : (
          <>
            <p className="sub" style={{ margin: 0 }}>
              It holds {[books && plural(books, "book"), below.length && plural(below.length, "sub-shelf", "sub-shelves")].filter(Boolean).join(" and ")}
              {shelf.grant_count ? `, and is shared with ${plural(shelf.grant_count, "person", "people")} (they lose access through it)` : ""}.
            </p>
            <fieldset className="choice-list">
              <legend className="sr-only">What happens to what is in it</legend>
              <label className="choice">
                <input type="radio" name="contents" checked={choice === "move_up"} onChange={() => setChoice("move_up")} data-autofocus />
                <span>
                  <strong>Move its contents up to {up}</strong>
                  <span className="hint">{parent ? `Its sub-shelves and books go into ${up}.` : "Its sub-shelves become top-level shelves; its books stay in the library (on no shelf, unless they are on another)."}</span>
                </span>
              </label>
              <label className="choice" aria-disabled={!canEdit || undefined}>
                <input type="radio" name="contents" checked={choice === "trash"} onChange={() => setChoice("trash")} disabled={!canEdit} />
                <span>
                  <strong>Move its {plural(books, "book")} to the Trash</strong>
                  <span className="hint">Every book in it or below it, also off any other shelf. Restorable for 30 days. Its sub-shelves are deleted too.</span>
                </span>
              </label>
            </fieldset>
          </>
        )}
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn danger" disabled={busy} onClick={run}>{empty || choice === "move_up" ? "Delete shelf" : "Delete and trash books"}</button>
        </div>
      </div>
    </Sheet>
  );
}

/** Depth this person can see for `folder` (1 = top level), for "New shelf" here. */
export function canNestIn(shelves: Shelf[], folder: FolderId): boolean {
  return folder === null || ancestry(shelves, folder).length < MAX_DEPTH;
}
