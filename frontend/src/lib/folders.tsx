// Moving and adding books and shelves between folders, from a drop (Library grid, breadcrumbs,
// the sidebar tree) or from "Move to…" (lib/shelves has the rules). A plain drop or Move
// takes the items out of the folder they came from; Alt/Option-drop or "Add" puts books on
// the target shelf as well (a book can be on several shelves). Shelves always move: a shelf
// has one parent. The root folder means "on no shelf". One toast, with Undo for all of it.

import { useCallback } from "react";
import { del, patch, post } from "./api";
import { bulkSummary } from "./editor";
import { byId, canHoldBooks, moveBlocked, type DropMode, type FolderId } from "./shelves";
import { useStore } from "./store";
import type { BulkResult } from "./types";
import type { BulkTarget } from "../components/BulkBar";

/** What is being moved: shelves by id, and books (explicit ids or a server-side selection). */
export interface Items {
  shelves: number[];
  works: BulkTarget | null;
  /** How many books `works` holds. */
  workCount: number;
  /** The folder they are shown in (null: the root); "all": the flat Everything view. */
  from: FolderId | "all";
}

const uuid = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
const n = (k: number, one: string, many = `${one}s`) => `${k} ${k === 1 ? one : many}`;

export function useFolderOps() {
  const { shelves, canCreateShelf, toast, refreshLibrary } = useStore();

  /** Why `items` cannot go to `to` this way, or null. */
  const blocked = useCallback(
    (items: Items, to: FolderId, mode: DropMode): string | null => {
      if (!items.shelves.length && !items.workCount) return "Nothing selected";
      if (mode === "add") {
        if (items.shelves.length) return "Shelves can only be moved (drop without Alt/Option)";
        if (to === null) return "Choose a shelf to add to";
      }
      if (mode === "move" && to === items.from) return "Already here";
      if (items.workCount && !canHoldBooks(shelves, to)) return "You can’t add books to that shelf";
      return moveBlocked(shelves, items.shelves, to, canCreateShelf);
    },
    [shelves, canCreateShelf],
  );

  const place = useCallback(
    async (items: Items, to: FolderId, mode: DropMode): Promise<boolean> => {
      const why = blocked(items, to, mode);
      if (why) {
        toast(why, "error");
        return false;
      }
      const m = byId(shelves);
      const where = to === null ? "the top level" : m.get(to)?.name ?? "that shelf";
      const movedShelves: { id: number; from: number | null }[] = [];
      const errors: string[] = [];
      for (const id of items.shelves) {
        try {
          await patch(`/api/shelves/${id}`, { parent_id: to });
          movedShelves.push({ id, from: m.get(id)?.parent_id ?? null });
        } catch (e) {
          errors.push(`${m.get(id)?.name ?? "A shelf"}: ${(e as Error).message}`);
        }
      }
      let batch: string | null = null;
      let books = { text: "", ok: 0, failed: 0 };
      if (items.works && items.workCount) {
        const body = mode === "add" ? { action: "add_to_shelves", shelf_ids: [to] } : { action: "move_to_shelf", from_shelf_id: items.from === "all" ? null : items.from, to_shelf_id: to };
        try {
          const r = await post<BulkResult>("/api/works/bulk", { ...items.works, ...body }, { "idempotency-key": uuid() });
          batch = r.batch_id;
          books = bulkSummary("", r.results);
          if (books.failed) errors.push(books.text.split("; ")[1] ?? "");
        } catch (e) {
          errors.push((e as Error).message);
        }
      }
      const parts = [movedShelves.length && n(movedShelves.length, "shelf", "shelves"), books.ok && n(books.ok, "book")].filter(Boolean);
      const undo = async () => {
        try {
          if (batch) await post(`/api/works/bulk/${batch}/undo`);
          for (const s of [...movedShelves].reverse()) await patch(`/api/shelves/${s.id}`, { parent_id: s.from });
          toast("Undone.");
        } catch (e) {
          toast((e as Error).message, "error");
        }
        await refreshLibrary().catch(() => {});
      };
      if (parts.length) {
        // From Everything there is no current folder to leave, so a move onto a shelf adds.
        const adds = mode === "add" || (items.from === "all" && to !== null && !movedShelves.length);
        const verb = adds ? "Added" : to === null && !movedShelves.length ? "Took" : "Moved";
        const tail = adds ? `to ${where}` : to === null && !movedShelves.length ? "off their shelves" : `to ${where}`;
        toast(`${verb} ${parts.join(" and ")} ${tail}.${errors.length ? ` ${errors.join("; ")}` : ""}`, errors.length ? "error" : "info", { label: "Undo", run: undo });
      } else if (errors.length) toast(errors.join("; "), "error");
      await refreshLibrary().catch(() => {});
      return parts.length > 0;
    },
    [blocked, shelves, toast, refreshLibrary],
  );

  return { blocked, place };
}

// --- drag and drop ---------------------------------------------------------------------------

/** The items being dragged inside the app (the DataTransfer only carries a marker: its data
 * cannot be read until the drop, and drop targets must decide on dragover). */
let current: Items | null = null;
const MIME = "application/x-lectern-items";

export function startDrag(e: React.DragEvent, items: Items, label: string) {
  current = items;
  e.dataTransfer.effectAllowed = "copyMove";
  e.dataTransfer.setData(MIME, "1");
  e.dataTransfer.setData("text/plain", label);
  const count = items.shelves.length + items.workCount;
  if (count > 1) {
    // A small "3 items" badge instead of a ghost of the one cell under the pointer.
    const ghost = document.createElement("div");
    ghost.className = "drag-ghost";
    ghost.textContent = label;
    document.body.appendChild(ghost);
    e.dataTransfer.setDragImage(ghost, 16, 16);
    setTimeout(() => ghost.remove(), 0);
  }
}

export function endDrag() {
  current = null;
}

/** Props for a folder that accepts drops: `data-drop` is "ok" or "no" while something is over
 * it (styles), and the drop runs `place`. Alt/Option at the moment of the drop adds. */
export function useDropTarget() {
  const { blocked, place } = useFolderOps();
  return useCallback(
    (to: FolderId) => ({
      onDragOver: (e: React.DragEvent<HTMLElement>) => {
        if (!current || !e.dataTransfer.types.includes(MIME)) return;
        const mode: DropMode = e.altKey ? "add" : "move";
        const ok = !blocked(current, to, mode);
        e.currentTarget.dataset.drop = ok ? "ok" : "no";
        if (!ok) return;
        e.preventDefault();
        e.stopPropagation();
        e.dataTransfer.dropEffect = mode === "add" ? "copy" : "move";
      },
      onDragLeave: (e: React.DragEvent<HTMLElement>) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) delete e.currentTarget.dataset.drop;
      },
      onDrop: (e: React.DragEvent<HTMLElement>) => {
        delete e.currentTarget.dataset.drop;
        const items = current;
        if (!items || !e.dataTransfer.types.includes(MIME)) return;
        e.preventDefault();
        e.stopPropagation();
        current = null;
        void place(items, to, e.altKey ? "add" : "move");
      },
    }),
    [blocked, place],
  );
}

/** Delete a shelf; `contents` for a non-empty one (docs/contracts.md "Shelves"). */
export async function deleteShelf(id: number, contents: "move_up" | "trash" | null): Promise<{ batch_id: string | null; deleted: boolean } | null> {
  return del(`/api/shelves/${id}${contents ? `?contents=${contents}` : ""}`);
}
