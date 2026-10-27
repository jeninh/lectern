import { useState } from "react";
import { post } from "../lib/api";
import { fieldLabel, fieldValue, fixSummary, offered, statusText, toggle } from "../lib/autometa";
import { useStore } from "../lib/store";
import type { AutoField, FixStatus, MetaSuggestionItem, MetaSuggestions } from "../lib/types";

const clip = (t: string) => (t.length > 240 ? t.slice(0, 240) + "…" : t);

/** The Fix metadata pass's results: each book's suggestions for the fields it was missing,
 * field by field like the editor's, applied as an ordinary (undoable) edit or dismissed. */
export function MetadataReview({ data, onChanged }: { data: MetaSuggestions | null; onChanged: () => Promise<void> }) {
  const { toast, me } = useStore();
  const [busy, setBusy] = useState(false);
  if (!data) return <p className="hint">Loading…</p>;
  const ready = data.items.filter((i) => i.status === "ready");
  const others = data.items.filter((i) => i.status !== "ready");
  const unavailable = !data.lookup_enabled
    ? me?.user.role === "owner"
      ? "Metadata suggestions are off. Turn them on in Admin to look up missing details."
      : "Metadata suggestions are off. The owner can turn them on in Admin."
    : data.lookup_offline
      ? "Offline mode is on: Lectern makes no internet connections, so nothing is looked up."
      : null;

  const wholeLibrary = async () => {
    setBusy(true);
    try {
      const r = await post<{ counts: Partial<Record<FixStatus, number>> }>("/api/metadata/fix", { library: true });
      toast(fixSummary(null, r.counts).text);
      await onChanged();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack">
      <p className="sub" style={{ margin: 0 }}>
        Description, series, ISBN, and subjects from Open Library and Google Books, for books that are missing them. Nothing changes until you apply it, and fields you have edited are never looked up.
      </p>
      {unavailable ? (
        <div className="alert warn" role="status">{unavailable}</div>
      ) : (
        <div className="row">
          <button className="btn" disabled={busy} onClick={wholeLibrary}>Fix metadata for the whole library</button>
          {!!data.counts.queued && <span className="hint" role="status">Looking up {data.counts.queued} {data.counts.queued === 1 ? "book" : "books"}…</span>}
        </div>
      )}
      {ready.length === 0 && others.length === 0 && <p className="hint">Nothing to review. Select books and choose Fix metadata, or run it for the whole library.</p>}
      {ready.length > 0 && (
        <ul className="review-list" aria-label="Suggestions to review">
          {ready.map((it) => <ReviewItem key={`${it.work_id}:${it.updated_at}`} item={it} onDone={onChanged} />)}
        </ul>
      )}
      {others.length > 0 && (
        <details open={ready.length === 0}>
          <summary>Other results ({others.length})</summary>
          <ul className="list" style={{ marginTop: 8 }}>
            {others.map((it) => (
              <li key={it.work_id} className="row" style={{ justifyContent: "space-between" }}>
                <span><strong>{it.title}</strong> <span className="hint">{it.author} · {statusText(it)}</span></span>
                {it.status !== "queued" && <DismissButton item={it} onDone={onChanged} label="Clear" />}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function DismissButton({ item, onDone, label = "Dismiss" }: { item: MetaSuggestionItem; onDone: () => Promise<void>; label?: string }) {
  const { toast } = useStore();
  return (
    <button
      className="btn sm ghost"
      onClick={async () => {
        try {
          await post(`/api/metadata/suggestions/${item.work_id}/dismiss`);
          await onDone();
        } catch (e) {
          toast((e as Error).message, "error");
        }
      }}
    >
      {label}
    </button>
  );
}

function ReviewItem({ item, onDone }: { item: MetaSuggestionItem; onDone: () => Promise<void> }) {
  const { toast, refreshLibrary } = useStore();
  // Opt-in, like the editor's suggestions: nothing is ticked until the person chooses.
  const [chosen, setChosen] = useState<AutoField[]>([]);
  const [busy, setBusy] = useState(false);
  const fields = offered(item);
  const apply = async () => {
    setBusy(true);
    try {
      await post(`/api/metadata/suggestions/${item.work_id}/apply`, { version: item.version, fields: chosen });
      toast(`Updated ${item.title}: ${chosen.map((f) => fieldLabel(f).toLowerCase()).join(", ")}`);
      await Promise.all([onDone(), refreshLibrary()]);
    } catch (e) {
      toast((e as Error).message, "error");
      setBusy(false);
    }
  };
  return (
    <li className="card suggestion" data-testid="metadata-suggestion">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span><strong>{item.title}</strong> <span className="hint">{item.author}</span></span>
      </div>
      <table className="list compare">
        <thead>
          <tr>
            <th scope="col"><span className="sr-only">Use</span></th>
            <th scope="col">Field</th>
            <th scope="col">Yours</th>
            <th scope="col">Suggested</th>
          </tr>
        </thead>
        <tbody>
          {fields.map((f) => {
            const s = item.fields[f]!;
            return (
              <tr key={f} data-chosen={chosen.includes(f) || undefined}>
                <td><input type="checkbox" aria-label={`Use suggested ${fieldLabel(f).toLowerCase()} for ${item.title}`} checked={chosen.includes(f)} onChange={() => setChosen((c) => toggle(c, f))} /></td>
                <th scope="row">{fieldLabel(f)}</th>
                <td className="was">{clip(fieldValue(f, item.current[f]))}</td>
                <td className="now">
                  {clip(fieldValue(f, s.value))}{" "}
                  <a className="hint" href={s.source_url} target="_blank" rel="noopener noreferrer nofollow">{s.provider_name}</a>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <DismissButton item={item} onDone={onDone} />
        <button className="btn sm ghost" onClick={() => setChosen(fields)}>Select all</button>
        <button className="btn sm primary" disabled={busy || !chosen.length} onClick={apply}>
          Apply {chosen.length || ""} {chosen.length === 1 ? "field" : "fields"}
        </button>
      </div>
    </li>
  );
}
