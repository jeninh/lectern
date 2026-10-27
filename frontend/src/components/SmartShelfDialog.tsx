// Save the Library's filter as a smart shelf, or change a smart shelf's filter and order
// (docs/filters.md). The server checks the filter again and evaluates it live for whoever
// opens the shelf: their books, their progress, their devices.

import { useEffect, useMemo, useState } from "react";
import { patch, post } from "../lib/api";
import { FilterError, parse, SORTS } from "../lib/filters";
import { pathLabel } from "../lib/shelves";
import { useStore } from "../lib/store";
import type { Shelf } from "../lib/types";
import { RuleChips } from "./FilterBar";
import { Sheet } from "./Sheet";

export function SmartShelfDialog({ open, onClose, onSaved, shelf, filter: initialFilter = "", sort: initialSort = "title", order: initialOrder = null, parent = null }: {
  open: boolean;
  onClose: () => void;
  onSaved: (s: Shelf) => void;
  /** Editing this smart shelf; otherwise a new one in `parent` (null: the top level). */
  shelf?: Shelf | null;
  filter?: string;
  sort?: string;
  order?: "asc" | "desc" | null;
  parent?: number | null;
}) {
  const { shelves, toast } = useStore();
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [sort, setSort] = useState("title");
  const [order, setOrder] = useState<"asc" | "desc" | "">("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!open) return;
    setName(shelf?.name ?? "");
    setText(shelf?.filter ?? initialFilter);
    setSort(shelf?.sort ?? initialSort);
    setOrder((shelf ? shelf.order : initialOrder) ?? "");
  }, [open, shelf, initialFilter, initialSort, initialOrder]);
  const parsed = useMemo(() => {
    try {
      const q = parse(text);
      return { clauses: q.clauses, words: q.words, error: q.clauses.length || q.words.length ? null : "Add at least one condition." };
    } catch (e) {
      return { clauses: [], words: [], error: e instanceof FilterError ? e.message : String(e) };
    }
  }, [text]);
  const where = parent === null ? "at the top level" : `in ${pathLabel(shelves.find((s) => s.id === parent) ?? { name: "this shelf", path: [] })}`;
  const save = async () => {
    setBusy(true);
    try {
      const body = { filter: text, sort, order: order || null };
      const out = shelf ? await patch<Shelf>(`/api/shelves/${shelf.id}`, body) : await post<Shelf>("/api/shelves", { ...body, name: name.trim(), parent_id: parent });
      onSaved(out);
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Sheet open={open} onClose={onClose} variant="dialog" title={shelf ? `Edit “${shelf.name}”` : "Save as smart shelf"} labelledBy="smart-title">
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          if (!parsed.error && (shelf || name.trim())) save();
        }}
      >
        <p className="sub" style={{ margin: 0 }}>
          {shelf
            ? "Its books are whatever matches this filter, kept up to date. Everyone who can see the shelf sees only the matching books they can already see."
            : `A shelf ${where} that keeps itself up to date: it holds whatever matches this filter, also on the Kindle (OPDS). People you share it with see only the matching books they can already see.`}
        </p>
        {!shelf && (
          <label className="field">
            <span>Name</span>
            <input className="input" value={name} maxLength={128} onChange={(e) => setName(e.target.value)} required data-autofocus autoFocus placeholder="Unread classics" />
          </label>
        )}
        <label className="field">
          <span>Filter</span>
          <input className="input mono" value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} aria-describedby="smart-filter-help" {...(shelf ? { autoFocus: true } : {})} />
        </label>
        <div id="smart-filter-help" className="smart-preview">
          {parsed.error ? <span className="field-error" role="alert">{parsed.error}</span> : <RuleChips clauses={parsed.clauses} />}
          {parsed.words.length > 0 && <span className="hint">and the words {parsed.words.map((w) => `${w.negate ? "not " : ""}“${w.text}”`).join(", ")}</span>}
        </div>
        <div className="row">
          <label className="field" style={{ flex: 1 }}>
            <span>Order</span>
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
          <button className="btn primary" disabled={busy || !!parsed.error || (!shelf && !name.trim())}>{shelf ? "Save" : "Save shelf"}</button>
        </div>
      </form>
    </Sheet>
  );
}
