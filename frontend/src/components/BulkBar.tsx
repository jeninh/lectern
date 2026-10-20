import { useRef, useState } from "react";
import { post } from "../lib/api";
import { bulkSummary } from "../lib/editor";
import { useStore } from "../lib/store";
import type { BulkResult, Shelf } from "../lib/types";
import { Close, Send, Trash } from "./Icons";
import { ChipInput } from "./MetadataForm";
import { Sheet } from "./Sheet";

type Action = "author" | "series" | "tags" | "shelves";
const uuid = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;

/** Sticky bar for the Library's selection mode: one change to many books, with Undo. */
export function BulkBar({ ids, visible, shelves, onSelectAll, onDone, onClose, canEdit = true, onSend }: { ids: number[]; visible: number[]; shelves: Shelf[]; onSelectAll: (ids: number[]) => void; onDone: () => Promise<void>; onClose: () => void; canEdit?: boolean; onSend?: () => void }) {
  const { toast, works } = useStore();
  const [dialog, setDialog] = useState<Action | null>(null);
  const [busy, setBusy] = useState(false);
  const n = ids.length;
  const allPicked = visible.length > 0 && visible.every((id) => ids.includes(id));

  const run = async (body: Record<string, unknown>, verb: string) => {
    setBusy(true);
    try {
      const r = await post<BulkResult>("/api/works/bulk", { work_ids: ids, ...body }, { "idempotency-key": uuid() });
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
      await onDone();
      if (body.action === "trash") onClose();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  const titles = works.filter((w) => ids.includes(w.id)).map((w) => w.title);
  return (
    <>
      <div className="bulk-bar" role="toolbar" aria-label={`${n} selected`}>
        <strong aria-live="polite">{n ? `${n} selected` : "Select books"}</strong>
        <button className="btn sm ghost" onClick={() => onSelectAll(allPicked ? [] : visible)}>{allPicked ? "Select none" : `Select all ${visible.length}`}</button>
        <span className="spacer" />
        {onSend && <button className="btn sm" disabled={!n} onClick={onSend}><Send /> Send to device</button>}
        {canEdit && (
          <>
            <button className="btn sm" disabled={!n} onClick={() => setDialog("author")}>Author</button>
            <button className="btn sm" disabled={!n} onClick={() => setDialog("series")}>Series</button>
            <button className="btn sm" disabled={!n} onClick={() => setDialog("tags")}>Tags</button>
            {shelves.length > 0 && <button className="btn sm" disabled={!n} onClick={() => setDialog("shelves")}>Shelves</button>}
            <button
              className="btn sm danger"
              disabled={!n || busy}
              onClick={() => window.confirm(`Move ${n} ${n === 1 ? "book" : "books"} to the trash? You can restore them for 30 days.`) && run({ action: "trash" }, "Moved to trash:")}
            >
              <Trash /> Trash
            </button>
          </>
        )}
        <button className="icon-btn ghost sm" onClick={onClose} aria-label="Leave selection mode"><Close /></button>
      </div>
      <Sheet open={dialog !== null} onClose={() => setDialog(null)} title={dialog ? `${dialog === "author" ? "Set author" : dialog === "series" ? "Set series" : dialog === "tags" ? "Tags" : "Shelves"} for ${n} ${n === 1 ? "book" : "books"}` : ""} variant="dialog" labelledBy="bulk-title">
        {dialog && <BulkForm action={dialog} shelves={shelves} busy={busy} titles={titles} onSubmit={run} />}
      </Sheet>
    </>
  );
}

function BulkForm({ action, shelves, busy, titles, onSubmit }: { action: Action; shelves: Shelf[]; busy: boolean; titles: string[]; onSubmit: (body: Record<string, unknown>, verb: string) => void }) {
  const [author, setAuthor] = useState("");
  const [series, setSeries] = useState("");
  const [index, setIndex] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [shelfIds, setShelfIds] = useState<number[]>([]);
  const mode = useRef<"add" | "remove">("add");
  const preview = (
    <p className="hint" style={{ margin: 0 }}>
      {titles.slice(0, 3).join(", ")}{titles.length > 3 ? ` and ${titles.length - 3} more` : ""}. Each book is saved on its own; Undo reverts them all.
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
          <label className="field index"><span>Number</span><input className="input" type="number" step="any" min={0} value={index} onChange={(e) => setIndex(e.target.value)} disabled={!series.trim() || titles.length > 1} title={titles.length > 1 ? "Set numbers one book at a time" : undefined} /></label>
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
