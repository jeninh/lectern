import { useRef, useState, type ReactNode } from "react";
import { post } from "../lib/api";
import { bulkSummary } from "../lib/editor";
import { MOD } from "../lib/hooks";
import { useStore } from "../lib/store";
import type { BulkResult, Shelf } from "../lib/types";
import { Close, Send, Trash } from "./Icons";
import { ChipInput } from "./MetadataForm";
import { Sheet } from "./Sheet";

type Action = "author" | "series" | "tags" | "shelves";
const uuid = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;

/** What a bulk request targets: explicit ids, or the server-resolved view minus exclusions
 * (Cmd/Ctrl+A over items that may not be loaded; lectern.routes.api.selection). */
export type BulkTarget = { work_ids: number[] } | { selection: { filter: { q: string; status: string; shelf: number | null }; exclude_ids: number[]; expected_count: number } };

/** The sticky bar shared by every multi-select view: the count, select all, and clear. */
export function SelectionBar({ count, all, total, onSelectAll, onClear, children, label = "Selection" }: { count: number; all: boolean; total: number; onSelectAll: () => void; onClear: () => void; children?: ReactNode; label?: string }) {
  return (
    <div className="bulk-bar" role="toolbar" aria-label={label} data-testid="selection-bar">
      <strong aria-live="polite" data-testid="selection-count">{all ? `All ${count} selected` : count ? `${count} selected` : "Select items"}</strong>
      {!all && total > count && (
        <button className="btn sm ghost" onClick={onSelectAll} title={`Select all (${MOD} A)`}>Select all {total}</button>
      )}
      <span className="spacer" />
      {children}
      <button className="icon-btn ghost sm" onClick={onClear} aria-label="Clear selection" title="Clear selection (Esc)"><Close /></button>
    </div>
  );
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** The Library's selection bar: one change to many books, with Undo. */
export function BulkBar({ count, all, total, target, titles, shelves, onSelectAll, onClear, onDone, canEdit = true, canAlign = false, onSend }: { count: number; all: boolean; total: number; target: () => BulkTarget; titles: string[]; shelves: Shelf[]; onSelectAll: () => void; onClear: () => void; onDone: () => Promise<void>; canEdit?: boolean; canAlign?: boolean; onSend?: () => void }) {
  const { toast } = useStore();
  const [dialog, setDialog] = useState<Action | null>(null);
  const [busy, setBusy] = useState(false);
  const n = count;

  const run = async (body: Record<string, unknown>, verb: string) => {
    setBusy(true);
    try {
      const r = await post<BulkResult>("/api/works/bulk", { ...target(), ...body }, { "idempotency-key": uuid() });
      const { text, ok, failed } = bulkSummary(verb, r.results);
      toast(
        text,
        failed && !ok ? "error" : "info",
        ok
          ? {
              label: "Undo",
              run: async () => {
                try {
                  const u = await post<{ results: BulkResult["results"] }>(`/api/works/bulk/${r.batch_id}/undo`);
                  const s = bulkSummary("Undid", u.results);
                  toast(s.failed ? s.text : "Undone.", s.failed && !s.ok ? "error" : "info");
                  await onDone();
                } catch (e) {
                  toast((e as Error).message, "error");
                }
              },
            }
          : undefined,
      );
      setDialog(null);
      if (body.action === "trash") onClear();
      await onDone();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  const align = async () => {
    setBusy(true);
    try {
      const r = await post<{ results: { work_id: number; status: string }[] }>("/api/works/align", target());
      const by = (st: string) => r.results.filter((x) => x.status === st).length;
      const parts = [
        by("queued") && `Queued alignment for ${plural(by("queued"), "book")}`,
        by("already_queued") && `${by("already_queued")} already queued`,
        by("no_inputs") && `${plural(by("no_inputs"), "book")} without both an EPUB and audio`,
        by("out_of_memory_choose_model") && `${plural(by("out_of_memory_choose_model"), "book")} ran out of memory last time (re-run from the editor with a smaller model)`,
        by("not_found") && `${by("not_found")} not found`,
      ].filter(Boolean);
      toast(parts.join(" · ") || "Nothing to align", by("queued") || by("already_queued") ? "info" : "error");
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <SelectionBar count={n} all={all} total={total} onSelectAll={onSelectAll} onClear={onClear} label={`${n} selected`}>
        {onSend && <button className="btn sm" disabled={!n} onClick={onSend}><Send /> Send</button>}
        {canEdit && (
          <>
            <button className="btn sm" disabled={!n} onClick={() => setDialog("author")}>Author</button>
            <button className="btn sm" disabled={!n} onClick={() => setDialog("series")}>Series</button>
            <button className="btn sm" disabled={!n} onClick={() => setDialog("tags")}>Tags</button>
            {shelves.length > 0 && <button className="btn sm" disabled={!n} onClick={() => setDialog("shelves")}>Shelves</button>}
          </>
        )}
        {canAlign && <button className="btn sm" disabled={!n || busy} onClick={align}>Align</button>}
        {canEdit && (
          <button
            className="btn sm danger"
            disabled={!n || busy}
            onClick={() => window.confirm(`Move ${plural(n, "book")} to the trash? You can restore them for 30 days.`) && run({ action: "trash" }, "Moved to trash:")}
          >
            <Trash /> Trash
          </button>
        )}
      </SelectionBar>
      <Sheet open={dialog !== null} onClose={() => setDialog(null)} title={dialog ? `${dialog === "author" ? "Set author" : dialog === "series" ? "Set series" : dialog === "tags" ? "Tags" : "Shelves"} for ${plural(n, "book")}` : ""} variant="dialog" labelledBy="bulk-title">
        {dialog && <BulkForm action={dialog} shelves={shelves} busy={busy} titles={titles} count={n} onSubmit={run} />}
      </Sheet>
    </>
  );
}

function BulkForm({ action, shelves, busy, titles, count, onSubmit }: { action: Action; shelves: Shelf[]; busy: boolean; titles: string[]; count: number; onSubmit: (body: Record<string, unknown>, verb: string) => void }) {
  const [author, setAuthor] = useState("");
  const [series, setSeries] = useState("");
  const [index, setIndex] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [shelfIds, setShelfIds] = useState<number[]>([]);
  const mode = useRef<"add" | "remove">("add");
  const preview = (
    <p className="hint" style={{ margin: 0 }}>
      {titles.slice(0, 3).join(", ")}{count > Math.min(3, titles.length) ? ` and ${count - Math.min(3, titles.length)} more` : ""}. Each book is saved on its own; Undo reverts them all.
    </p>
  );
  if (action === "author")
    return (
      <form className="stack" onSubmit={(e) => { e.preventDefault(); onSubmit({ action: "set_authors", authors: author.split(/\s*[&;]\s*/).filter(Boolean).map((name) => ({ name })) }, "Changed the author of"); }}>
        <label className="field"><span>Author (separate several with &amp;)</span><input className="input" value={author} onChange={(e) => setAuthor(e.target.value)} required maxLength={256} data-autofocus /></label>
        <p className="hint" style={{ margin: 0 }}>Book folders move to the new author; files keep their bytes, so devices stay in sync.</p>
        {preview}
        <div className="row" style={{ justifyContent: "flex-end" }}><button className="btn primary" disabled={busy || !author.trim()}>Set author</button></div>
      </form>
    );
  if (action === "series")
    return (
      <form className="stack" onSubmit={(e) => { e.preventDefault(); onSubmit({ action: "set_series", series: series.trim() || null, series_index: index === "" ? null : Number(index) }, series.trim() ? "Set the series of" : "Cleared the series of"); }}>
        <div className="field-pair">
          <label className="field"><span>Series (empty to clear)</span><input className="input" value={series} onChange={(e) => setSeries(e.target.value)} maxLength={256} data-autofocus /></label>
          <label className="field index"><span>Number</span><input className="input" type="number" step="any" min={0} value={index} onChange={(e) => setIndex(e.target.value)} disabled={!series.trim() || count > 1} title={count > 1 ? "Set numbers one book at a time" : undefined} /></label>
        </div>
        {preview}
        <div className="row" style={{ justifyContent: "flex-end" }}><button className="btn primary" disabled={busy}>{series.trim() ? "Set series" : "Clear series"}</button></div>
      </form>
    );
  if (action === "tags")
    return (
      <form className="stack" onSubmit={(e) => { e.preventDefault(); onSubmit({ action: mode.current === "add" ? "add_tags" : "remove_tags", tags }, mode.current === "add" ? "Tagged" : "Untagged"); }}>
        <label className="field" htmlFor="bulk-tags"><span>Tags</span><ChipInput id="bulk-tags" label="Tag" values={tags} onChange={setTags} placeholder="Type a tag and press Enter" /></label>
        {preview}
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button className="btn" disabled={busy || !tags.length} onClick={() => (mode.current = "remove")}>Remove these tags</button>
          <button className="btn primary" disabled={busy || !tags.length} onClick={() => (mode.current = "add")}>Add these tags</button>
        </div>
      </form>
    );
  return (
    <form className="stack" onSubmit={(e) => { e.preventDefault(); onSubmit({ action: mode.current === "add" ? "add_to_shelves" : "remove_from_shelves", shelf_ids: shelfIds }, mode.current === "add" ? "Shelved" : "Unshelved"); }}>
      <div className="chips" role="group" aria-label="Shelves">
        {shelves.map((s) => (
          <button key={s.id} type="button" className="chip subtle" aria-pressed={shelfIds.includes(s.id)} onClick={() => setShelfIds((x) => (x.includes(s.id) ? x.filter((y) => y !== s.id) : [...x, s.id]))}>
            {s.name}
          </button>
        ))}
      </div>
      {preview}
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button className="btn" disabled={busy || !shelfIds.length} onClick={() => (mode.current = "remove")}>Remove from shelves</button>
        <button className="btn primary" disabled={busy || !shelfIds.length} onClick={() => (mode.current = "add")}>Add to shelves</button>
      </div>
    </form>
  );
}
