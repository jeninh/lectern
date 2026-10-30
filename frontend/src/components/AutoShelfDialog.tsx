// Make an auto-shelves group (a shelf the library fills with one shelf per author, tag, genre,
// language, or narrator), or change a group's settings (docs/auto-shelves.md). The server
// checks every setting again and keeps the generated shelves up to date.

import { useEffect, useState } from "react";
import { patch, post } from "../lib/api";
import { SORTS } from "../lib/filters";
import { pathLabel } from "../lib/shelves";
import { useStore } from "../lib/store";
import type { AutoShelfField, AutoSettings, Shelf } from "../lib/types";
import { Sheet } from "./Sheet";

export const AUTO_FIELDS: { key: AutoShelfField; name: string; label: string; hint: string; minBooks: number; buckets: boolean }[] = [
  { key: "author", name: "Authors", label: "Author", hint: "One shelf per author, in A–Z groups.", minBooks: 2, buckets: true },
  { key: "tag", name: "Tags", label: "Tag", hint: "One shelf per tag, in A–Z groups.", minBooks: 2, buckets: true },
  { key: "genre", name: "Genres", label: "Genre", hint: "One shelf per genre the owner has mapped to tags (Admin).", minBooks: 1, buckets: false },
  { key: "lang", name: "Languages", label: "Language", hint: "One shelf per language.", minBooks: 1, buckets: false },
  { key: "narrator", name: "Narrators", label: "Narrator", hint: "One shelf per audiobook narrator.", minBooks: 1, buckets: false },
];

export function AutoShelfDialog({ open, onClose, onSaved, shelf, parent = null }: {
  open: boolean;
  onClose: () => void;
  onSaved: (s: Shelf) => void;
  /** Changing this group's settings; otherwise a new group in `parent` (null: the top level). */
  shelf?: Shelf | null;
  parent?: number | null;
}) {
  const { shelves, toast, refreshLibrary } = useStore();
  const [field, setField] = useState<AutoShelfField>("author");
  const [name, setName] = useState("Authors");
  const [named, setNamed] = useState(false);
  const [minBooks, setMinBooks] = useState(2);
  const [childOrder, setChildOrder] = useState<"name" | "count">("name");
  const [buckets, setBuckets] = useState(true);
  const [sort, setSort] = useState("title");
  const [order, setOrder] = useState<"asc" | "desc" | "">("");
  const [busy, setBusy] = useState(false);
  const spec = AUTO_FIELDS.find((f) => f.key === field)!;

  useEffect(() => {
    if (!open) return;
    const a = shelf?.auto;
    const f = AUTO_FIELDS.find((x) => x.key === (a?.field ?? "author"))!;
    setField(f.key);
    setName(shelf?.name ?? f.name);
    setNamed(!!shelf);
    setMinBooks(a?.min_books ?? f.minBooks);
    setChildOrder(a?.child_order ?? "name");
    setBuckets(a?.buckets ?? f.buckets);
    setSort(a?.sort ?? "title");
    setOrder(a?.order ?? "");
  }, [open, shelf]);

  const pick = (k: AutoShelfField) => {
    const f = AUTO_FIELDS.find((x) => x.key === k)!;
    setField(k);
    if (!named) setName(f.name);
    setMinBooks(f.minBooks);
    setBuckets(f.buckets);
  };
  const where = parent === null ? "at the top level" : `in ${pathLabel(shelves.find((s) => s.id === parent) ?? { name: "this shelf", path: [] })}`;
  const valid = minBooks >= 1 && minBooks <= 50 && (!!shelf || !!name.trim());
  const save = async () => {
    setBusy(true);
    try {
      const auto: AutoSettings = { min_books: minBooks, child_order: childOrder, buckets, sort, order: order || null };
      const out = shelf
        ? await patch<Shelf>(`/api/shelves/${shelf.id}`, { auto: { ...auto, field } })
        : await post<Shelf>("/api/shelves", { name: name.trim(), parent_id: parent, auto: { ...auto, field } });
      await refreshLibrary().catch(() => {});
      onSaved(out);
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Sheet open={open} onClose={onClose} variant="dialog" title={shelf ? `Settings for “${shelf.name}”` : "New auto-shelves"} labelledBy="auto-title">
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid) save();
        }}
      >
        <p className="sub" style={{ margin: 0 }}>
          {shelf
            ? "The library keeps the shelves under this one up to date. People see only the shelves for books they can already see."
            : `A shelf ${where} that fills itself: the library adds a shelf for each value, and keeps them up to date, also on the Kindle (OPDS).`}
        </p>
        {!shelf && (
          <label className="field">
            <span>Name</span>
            <input className="input" value={name} maxLength={128} pattern="[^/]+" title="A shelf name cannot contain /" onChange={(e) => { setName(e.target.value); setNamed(true); }} required data-autofocus autoFocus />
          </label>
        )}
        <label className="field">
          <span>One shelf for each</span>
          <select className="select" value={field} disabled={!!shelf} onChange={(e) => pick(e.target.value as AutoShelfField)}>
            {AUTO_FIELDS.map((f) => <option key={f.key} value={f.key}>{f.label}</option>)}
          </select>
          <span className="hint">{spec.hint}{shelf ? " The field can’t change; make a new group for another." : ""}</span>
        </label>
        <div className="row">
          <label className="field" style={{ flex: 1 }}>
            <span>Fewest books</span>
            <input className="input" type="number" min={1} max={50} value={minBooks} onChange={(e) => setMinBooks(Number(e.target.value))} />
            <span className="hint">A value needs this many books you can see.</span>
          </label>
          <label className="field" style={{ flex: 1 }}>
            <span>List shelves by</span>
            <select className="select" value={childOrder} onChange={(e) => setChildOrder(e.target.value as "name" | "count")}>
              <option value="name">Name</option>
              <option value="count">Number of books</option>
            </select>
          </label>
        </div>
        <label className="choice">
          <input type="checkbox" checked={buckets} onChange={(e) => setBuckets(e.target.checked)} />
          <span>
            <strong>Group by first letter</strong>
            <span className="hint">A–Z shelves (and #) between this shelf and the values. Needs one more level of nesting.</span>
          </span>
        </label>
        <div className="row">
          <label className="field" style={{ flex: 1 }}>
            <span>Books ordered by</span>
            <select className="select" value={sort} onChange={(e) => setSort(e.target.value)}>
              {SORTS.map((x) => <option key={x.key} value={x.key}>{x.label}</option>)}
            </select>
          </label>
          <label className="field" style={{ flex: 1 }}>
            <span>Direction</span>
            <select className="select" value={order} onChange={(e) => setOrder(e.target.value as "asc" | "desc" | "")}>
              <option value="">Usual ({SORTS.find((x) => x.key === sort)?.desc ? "largest or newest first" : "A to Z, smallest first"})</option>
              <option value="asc">Smallest, oldest, or A first</option>
              <option value="desc">Largest, newest, or Z first</option>
            </select>
          </label>
        </div>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || !valid}>{shelf ? "Save" : "Create"}</button>
        </div>
      </form>
    </Sheet>
  );
}
