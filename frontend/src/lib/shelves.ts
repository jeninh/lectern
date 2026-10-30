// The Library as folders: shelves nest (`parent_id`), a book can be on several shelves, and a
// book on none of the shelves this person sees sits at the root. Pure helpers over the list
// `GET /api/shelves` returns (depth-first, children in name order), shared by the Library,
// the sidebar tree, the folder picker, and Cmd/Ctrl+K. The server enforces every rule here
// again (lectern.routes.api.shelves); these only keep the UI from offering what would fail.

import type { Shelf, Work } from "./types";

/** How deep shelves nest (lectern.access.MAX_DEPTH). */
export const MAX_DEPTH = 8;

/** A folder: a shelf id, or null for the Library's root. */
export type FolderId = number | null;

export function childrenOf(shelves: readonly Shelf[], parent: FolderId): Shelf[] {
  return shelves.filter((s) => s.parent_id === parent);
}

export function byId(shelves: readonly Shelf[]): Map<number, Shelf> {
  return new Map(shelves.map((s) => [s.id, s]));
}

/** `shelf` and the visible shelves above it, top first (breadcrumbs). */
export function ancestry(shelves: readonly Shelf[], id: number): Shelf[] {
  const m = byId(shelves);
  const out: Shelf[] = [];
  let s = m.get(id);
  while (s && out.length <= MAX_DEPTH) {
    out.unshift(s);
    s = s.parent_id === null ? undefined : m.get(s.parent_id);
  }
  return out;
}

/** Auto-shelves (docs/auto-shelves.md): the library keeps these shelves itself. A *group* is an
 * ordinary shelf you rename, move, share, and delete; its buckets (A–Z) and children (one per
 * value) are generated and never changed by hand. Nothing is ever put in any of the three. */
export const autoKind = (s: Pick<Shelf, "auto">) => s.auto?.kind ?? null;
export const isGenerated = (s: Pick<Shelf, "auto">) => autoKind(s) === "bucket" || autoKind(s) === "value";

export type ShelfAction = "rename" | "move" | "delete" | "grant" | "settings" | "addTo" | "subShelf" | "drag";

/** Whether the person may do `action` to `s` (the server enforces it again: `409 auto_shelf`). */
export function shelfCan(s: Shelf, action: ShelfAction): boolean {
  const owner = s.permission === "owner";
  switch (action) {
    case "rename":
    case "move":
    case "delete":
    case "grant":
    case "drag":
      return owner && !isGenerated(s);
    case "settings":
      return owner && autoKind(s) === "group";
    case "addTo":
    case "subShelf":
      return s.permission !== "view" && !s.smart && autoKind(s) === null;
  }
}

/** Open folders after the sidebar tree makes room for the active one: groups and buckets stay
 * collapsed until something inside is open, then only the chain down to it expands. */
export function expandFor(shelves: readonly Shelf[], active: number | null | undefined, open: readonly number[]): number[] {
  if (!active) return [...open];
  return [...new Set([...open, ...ancestry(shelves, active).slice(0, -1).map((x) => x.id)])];
}

/** "Authors › Leo Tolstoy": the group and the value, without the A–Z bucket between them. */
export function autoPathLabel(shelves: readonly Shelf[], s: Shelf): string {
  const chain = ancestry(shelves, s.id).filter((x) => autoKind(x) !== "bucket" || x.id === s.id);
  return (chain.length ? chain : [s]).map((x) => x.name).join(" › ");
}

export const pathLabel = (s: Pick<Shelf, "path" | "name">) => (s.path?.length ? s.path : [s.name]).join(" / ");

/** Whether `id` is `ancestor` or sits somewhere below it. */
export function isWithin(shelves: readonly Shelf[], id: number, ancestor: number): boolean {
  return ancestry(shelves, id).some((s) => s.id === ancestor);
}

/** Levels in the subtree under `id`, itself included (1 = no sub-shelves). */
export function height(shelves: readonly Shelf[], id: number, seen = new Set<number>()): number {
  if (seen.has(id)) return 0;
  seen.add(id);
  return 1 + Math.max(0, ...childrenOf(shelves, id).map((c) => height(shelves, c.id, seen)));
}

/** The works directly in a folder: on that shelf, or (root) on none of the visible ones. */
export function worksIn(works: readonly Work[], folder: FolderId): Work[] {
  return works.filter((w) => (folder === null ? !(w.shelf_ids?.length) : !!w.shelf_ids?.includes(folder)));
}

/** Why `shelves` (ids) cannot move into `target`, or null if they can. `canCreate`: may make
 * top-level shelves. Depth is checked against the levels this person can see; the server
 * also counts levels above that they cannot. */
export function moveBlocked(all: readonly Shelf[], moving: readonly number[], target: FolderId, canCreate: boolean): string | null {
  if (!moving.length) return null;
  const m = byId(all);
  if (target === null) return canCreate ? null : "You can’t make top-level shelves";
  const t = m.get(target);
  if (!t) return "Not found";
  if (!shelfCan(t, "subShelf")) return `You can’t add to ${t.name}`;
  for (const id of moving) {
    const s = m.get(id);
    if (!s) continue;
    if (isGenerated(s)) return `${s.name} is kept up to date by the library, so it can’t be moved`;
    if (s.permission !== "owner") return `Only its owner can move ${s.name}`;
    if (isWithin(all, target, id)) return id === target ? `Can’t move ${s.name} into itself` : `Can’t move ${s.name} into a shelf inside it`;
    if (ancestry(all, target).length + height(all, id) > MAX_DEPTH) return `Shelves nest at most ${MAX_DEPTH} deep`;
  }
  return null;
}

/** Whether books can be put on `target` (root: always, it means "off the shelf"). */
export function canHoldBooks(all: readonly Shelf[], target: FolderId): boolean {
  if (target === null) return true;
  const t = byId(all).get(target);
  return !!t && shelfCan(t, "addTo");
}

/** What a drop does: Alt (Option on a Mac) adds a copy of the membership; otherwise the
 * items leave the folder they came from. Folders only ever move (one parent each). */
export type DropMode = "move" | "add";
export const dropMode = (e: { altKey: boolean }): DropMode => (e.altKey ? "add" : "move");

/** Selection keys mix folders and books: "s12" is shelf 12, "w34" work 34. */
export const shelfKey = (id: number) => `s${id}`;
export const workKey = (id: number) => `w${id}`;
export function splitKeys(keys: Iterable<string>): { shelves: number[]; works: number[] } {
  const shelves: number[] = [];
  const works: number[] = [];
  for (const k of keys) (k[0] === "s" ? shelves : works).push(Number(k.slice(1)));
  return { shelves, works };
}
