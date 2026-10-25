import { useEffect, useMemo, useRef, useState } from "react";
import { bulkPlan, proposedTarget, type Choice } from "../lib/reviewBulk";
import { useSelection, type CellProps } from "../lib/useSelection";
import { SelectionBar } from "./BulkBar";
import { ApiError, del, post } from "../lib/api";
import { bytes } from "../lib/format";
import { clean, validate } from "../lib/metadata";
import { clock } from "../lib/player";
import { useStore } from "../lib/store";
import type { ImportInfo, Metadata, Proposal, Work } from "../lib/types";
import { Cover } from "./Cover";
import { Book, Close, Headphones, Pencil } from "./Icons";
import { MetadataForm } from "./MetadataForm";
import { Sheet } from "./Sheet";

const CONFIDENCE: Record<Proposal["confidence"], { label: string; cls: string }> = {
  high: { label: "Certain match", cls: "ok" },
  medium: { label: "Likely match", cls: "warn" },
  low: { label: "Possible match", cls: "" },
};

const uuid = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;

/** Every uploaded book waiting to be filed; opened from the tray, a toast, the Library, or Cmd+K. */
export function ReviewSheet() {
  const { reviewOpen, setReviewOpen, imports, refreshImports, refreshLibrary, me, setPreference, toast } = useStore();
  const open = imports.filter((i) => i.status === "ready" || i.status === "uploading" || i.status === "failed");
  useEffect(() => {
    if (reviewOpen) refreshImports().catch(() => {});
  }, [reviewOpen, refreshImports]);
  // Ready uploads can be selected together and filed or discarded at once.
  const [scope, setScope] = useState<HTMLDivElement | null>(null);
  const ready = useMemo(() => open.filter((i) => i.status === "ready"), [open]);
  const keys = useMemo(() => ready.map((i) => i.id), [ready]);
  const s = useSelection<string>({ scope, keys, global: true });
  const [busy, setBusy] = useState(false);
  const unsaved = useRef(new Map<string, boolean>());
  const markers = useMemo(() => new Map<string, (u: boolean) => void>(), []);
  const marker = (id: string) => {
    if (!markers.has(id)) markers.set(id, (u) => unsaved.current.set(id, u));
    return markers.get(id)!;
  };

  const bulk = async (verb: "file" | "discard") => {
    const picked = ready.filter((i) => s.isSelected(i.id));
    if (verb === "discard" && !window.confirm(`Discard ${picked.length} ${picked.length === 1 ? "upload" : "uploads"}? Their uploaded files are deleted.`)) return;
    setBusy(true);
    let ok = 0;
    const failed: string[] = [];
    const skipped: Record<"unsaved" | "uncertain" | "details", string[]> = { unsaved: [], uncertain: [], details: [] };
    for (const imp of picked) {
      const name = imp.metadata?.title || imp.detected?.title || imp.label;
      try {
        if (verb === "discard") await del(`/api/imports/${imp.id}`);
        else {
          const plan = bulkPlan(imp, !!unsaved.current.get(imp.id));
          if ("skip" in plan) {
            skipped[plan.skip].push(name);
            continue;
          }
          await post(`/api/imports/${imp.id}/file`, { version: imp.version, metadata: clean(imp.metadata!), target: plan.target, use_cover: imp.use_cover ?? true, audio: "refuse" }, { "idempotency-key": uuid() });
        }
        ok += 1;
      } catch {
        failed.push(name);
      }
    }
    setBusy(false);
    const list = (xs: string[]) => `${xs.slice(0, 3).join(", ")}${xs.length > 3 ? ` and ${xs.length - 3} more` : ""}`;
    const notes = [
      skipped.unsaved.length && `left ${list(skipped.unsaved)} (unsaved changes: file ${skipped.unsaved.length === 1 ? "it" : "them"} from the card)`,
      skipped.uncertain.length && `left ${list(skipped.uncertain)} (not a certain match: choose where ${skipped.uncertain.length === 1 ? "it goes" : "they go"})`,
      skipped.details.length && `left ${list(skipped.details)} (details need fixing)`,
      failed.length && `could not file ${list(failed)}`,
    ].filter(Boolean);
    const left = failed.length + skipped.unsaved.length + skipped.uncertain.length + skipped.details.length;
    toast(`${verb === "file" ? "Filed" : "Discarded"} ${ok} ${ok === 1 ? "book" : "books"}${notes.length ? ` · ${notes.join(" · ")}` : ""}.`, left && !ok ? "error" : "info");
    s.exit();
    await Promise.all([refreshImports(), refreshLibrary()]).catch(() => {});
  };
  return (
    <Sheet open={reviewOpen} onClose={() => setReviewOpen(false)} title="Review before filing" labelledBy="import-review-title" wide>
      <div className="stack">
        <p className="sub" style={{ margin: 0 }}>
          Check what Lectern detected, then file each book. Nothing reaches your library until you do.
        </p>
        <div className="row pref-row">
          <button
            type="button"
            role="switch"
            className="switch"
            aria-checked={!!me?.preferences.auto_file_imports}
            aria-labelledby="autofile-label"
            onClick={() => setPreference("auto_file_imports", !me?.preferences.auto_file_imports)}
          >
            <span aria-hidden="true" />
          </button>
          <span id="autofile-label">
            <strong>File certain matches automatically</strong>
            <span className="hint"> Only when the ISBN or the exact title and author match a book you already have. Everything else still waits here.</span>
          </span>
        </div>
        {open.length === 0 ? (
          <div className="empty-review">
            <Book />
            <strong>Nothing waiting</strong>
            <span className="hint">Add books from the Library, or drop files anywhere on the page.</span>
          </div>
        ) : (
          <div className="import-list" role="grid" aria-label="Uploads waiting" aria-multiselectable="true" aria-rowcount={open.length} ref={setScope} {...s.scopeProps}>
            {open.map((imp, i) => (
              <div role="row" key={imp.id} aria-rowindex={i + 1}>
                <ImportCard imp={imp} siblings={open.filter((o) => o.id !== imp.id && o.status === "ready")} cell={imp.status === "ready" ? s.cellProps(imp.id) : undefined} onUnsaved={marker(imp.id)} />
              </div>
            ))}
          </div>
        )}
        {(
          <SelectionBar open={s.count > 0 || s.selecting} count={s.count} all={s.allSelected} total={keys.length} onSelectAll={s.selectAll} onClear={s.exit} label="Selected uploads">
            <button className="btn sm ghost" disabled={busy || !s.count} onClick={() => bulk("discard")}><Close /> Discard</button>
            <button className="btn sm primary" disabled={busy || !s.count} onClick={() => bulk("file")}>File {s.count}</button>
          </SelectionBar>
        )}
      </div>
    </Sheet>
  );
}

function ImportCard({ imp, siblings, cell, onUnsaved }: { imp: ImportInfo; siblings: ImportInfo[]; cell?: CellProps; onUnsaved?: (unsaved: boolean) => void }) {
  const { works, toast, refreshImports, refreshLibrary, openEditor, setReviewOpen } = useStore();
  const proposal = imp.proposal;
  const [choice, setChoice] = useState<Choice>(() => proposedTarget(imp));
  const [editing, setEditing] = useState(false);
  const [meta, setMeta] = useState<Metadata | null>(imp.metadata);
  const [useCover, setUseCover] = useState(imp.use_cover ?? true);
  const [append, setAppend] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [needsAppend, setNeedsAppend] = useState(false);
  const [other, setOther] = useState("");
  const idem = useRef(uuid());
  // Choices made on this card and not filed yet: bulk filing leaves the card alone.
  const unsaved = JSON.stringify(choice) !== JSON.stringify(proposedTarget(imp)) || useCover !== (imp.use_cover ?? true) || append || JSON.stringify(meta) !== JSON.stringify(imp.metadata);
  useEffect(() => {
    onUnsaved?.(unsaved);
  }, [unsaved, onUnsaved]);
  useEffect(() => {
    // Server-side changes (combined files, re-detection) replace an untouched draft.
    if (!editing) setMeta(imp.metadata);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [imp.version]);

  const ebooks = imp.files.filter((f) => f.kind === "ebook");
  const tracks = imp.files.filter((f) => f.kind === "audio");
  const duration = tracks.reduce((a, t) => a + (t.duration_s ?? 0), 0);
  const dups = imp.files.filter((f) => f.status === "duplicate");
  const target = choice.action === "join" ? proposal?.candidates.find((c) => c.work.id === choice.work_id)?.work ?? works.find((w) => w.id === choice.work_id) : null;
  const targetHasAudio = target ? ("has_audio" in target ? target.has_audio : !!(target as Work).audio) : false;
  const errors = useMemo(() => (meta ? validate(meta) : {}), [meta]);
  const invalid = Object.keys(errors).length > 0;
  const shown = meta ?? imp.detected;
  const candidateIds = new Set(proposal?.candidates.map((c) => c.work.id));

  const file = async () => {
    if (!meta || invalid) {
      setEditing(true);
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const r = await post<{ work_id: number; report: { created: boolean } }>(
        `/api/imports/${imp.id}/file`,
        { version: imp.version, metadata: clean(meta), target: choice, use_cover: useCover, audio: append ? "append" : "refuse" },
        { "idempotency-key": idem.current },
      );
      idem.current = uuid();
      toast(r.report.created ? `Added “${meta.title}” to your library.` : `Added to “${target?.title ?? meta.title}”.`, "info", { label: "Edit", run: () => { setReviewOpen(false); openEditor(r.work_id); } });
      await Promise.all([refreshImports(), refreshLibrary()]);
    } catch (e) {
      if (e instanceof ApiError && e.code === "target_has_audio") setNeedsAppend(true);
      if (e instanceof ApiError && e.code === "version_conflict") await refreshImports().catch(() => {});
      idem.current = uuid();
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const discard = async () => {
    if (!window.confirm(`Discard “${shown?.title || imp.label}”? Its uploaded files are deleted.`)) return;
    try {
      await del(`/api/imports/${imp.id}`);
      await refreshImports();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  const removeFile = async (fid: number) => {
    try {
      await del(`/api/imports/${imp.id}/files/${fid}`);
      await refreshImports();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  const combine = async (otherId: string) => {
    try {
      await post(`/api/imports/${imp.id}/combine`, { other_id: otherId });
      await refreshImports();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  if (imp.status === "uploading") {
    return (
      <div className="card import-card" role="gridcell" aria-selected={false} aria-disabled="true" aria-busy="true">
        <strong>{imp.label}</strong>
        <span className="hint">{imp.uploads_active ? `Uploading ${imp.uploads_active} more ${imp.uploads_active === 1 ? "file" : "files"}…` : "Waiting for files…"}</span>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button className="btn sm ghost" onClick={discard}>Discard</button>
        </div>
      </div>
    );
  }
  if (imp.status === "failed") {
    const dup = imp.files.find((f) => f.duplicate_of)?.duplicate_of;
    return (
      <div className="card import-card" role="gridcell" aria-selected={false} aria-disabled="true">
        <strong className="import-title">{imp.detected?.title || imp.label}</strong>
        <div className={`alert ${imp.error === "all_duplicates" ? "warn" : "error"}`}>
          {imp.error === "all_duplicates"
            ? <>These exact files are already in your library{dup ? <> as “{dup.title}”{dup.in_trash ? " (in the trash: restore it from Trash)" : ""}</> : null}. Nothing new to add.</>
            : "Lectern could not read these files. They may be damaged or DRM-protected."}
        </div>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button className="btn sm" onClick={async () => { await del(`/api/imports/${imp.id}`).catch(() => {}); await refreshImports(); }}>Dismiss</button>
        </div>
      </div>
    );
  }

  return (
    <div className="card import-card" aria-label={shown?.title || imp.label} {...cell} role="gridcell">
      {cell && <span className="pick" data-sel-toggle aria-hidden="true" title="Select">{cell["aria-selected"] ? "✓" : ""}</span>}
      <div className="import-head">
        {imp.cover_url && useCover ? (
          <div className="cover"><img src={imp.cover_url} alt="" /></div>
        ) : (
          <Cover work={{ title: shown?.title ?? imp.label, author: shown?.authors.map((a) => a.name).join(", ") ?? "", cover_url: null, cover_tint: null }} />
        )}
        <div className="stack" style={{ gap: 6, minWidth: 0 }}>
          <strong className="import-title">{shown?.title || imp.label}</strong>
          <span className="hint">{shown?.authors.map((a) => a.name).join(", ") || "Unknown author"}{shown?.series ? ` · ${shown.series}${shown.series_index != null ? ` #${shown.series_index}` : ""}` : ""}</span>
          <div className="row" style={{ gap: 6 }}>
            {ebooks.length > 0 && <span className="badge"><Book width={13} height={13} /> EPUB{ebooks.length > 1 ? ` ×${ebooks.length}` : ""}</span>}
            {tracks.length > 0 && <span className="badge"><Headphones width={13} height={13} /> {tracks.length} {tracks.length === 1 ? "file" : "tracks"}{duration ? ` · ${clock(duration)}` : ""}</span>}
            {shown?.language && <span className="badge">{shown.language}</span>}
            {shown?.isbn && <span className="badge">ISBN {shown.isbn}</span>}
            {shown?.narrators.length ? <span className="badge">Read by {shown.narrators.join(", ")}</span> : null}
          </div>
          {imp.cover_url && (
            <label className="check">
              <input type="checkbox" checked={useCover} onChange={(e) => setUseCover(e.target.checked)} /> Use the detected cover
            </label>
          )}
        </div>
      </div>

      {dups.length > 0 && (
        <div className="alert warn" role="note">
          {dups.length === imp.files.length ? "These exact files are" : `${dups.length} of these files ${dups.length === 1 ? "is" : "are"}`} already in your library
          {dups[0].duplicate_of ? <> as “{dups[0].duplicate_of.title}”{dups[0].duplicate_of.in_trash ? " (in the trash)" : ""}</> : null}. Duplicates are skipped when you file.
        </div>
      )}

      <fieldset className="match">
        <legend>Where should it go?</legend>
        {proposal && proposal.candidates.length > 0 && (
          <p className="hint" style={{ margin: "0 0 6px" }}>
            {proposal.action === "join" ? (
              <><span className={`badge ${CONFIDENCE[proposal.confidence].cls}`}>{CONFIDENCE[proposal.confidence].label}{proposal.score != null ? ` · ${Math.round(proposal.score)}%` : ""}</span> {proposal.reason}</>
            ) : (
              "No confident match. Lectern suggests a new book; similar ones are listed in case."
            )}
          </p>
        )}
        <div className="choices" role="radiogroup" aria-label="Where should it go?">
          {proposal?.candidates.map((c) => (
            <label key={c.work.id} className="choice" data-checked={choice.action === "join" && choice.work_id === c.work.id || undefined}>
              <input type="radio" name={`t-${imp.id}`} checked={choice.action === "join" && choice.work_id === c.work.id} onChange={() => setChoice({ action: "join", work_id: c.work.id })} />
              <Cover work={c.work} />
              <span className="titles">
                <strong>Add to “{c.work.title}”</strong>
                <span className="hint">{c.work.author}{c.work.has_ebook ? " · EPUB" : ""}{c.work.has_audio ? " · audiobook" : ""} · {Math.round(c.score)}% {c.certain ? "· certain" : ""}</span>
              </span>
            </label>
          ))}
          <label className="choice" data-checked={choice.action === "new" || undefined}>
            <input type="radio" name={`t-${imp.id}`} checked={choice.action === "new"} onChange={() => setChoice({ action: "new" })} />
            <span className="cover new-cover" aria-hidden="true">+</span>
            <span className="titles">
              <strong>Create a new book</strong>
              <span className="hint">Filed under {meta?.authors[0]?.name || "the author"} / {meta?.title || "the title"}</span>
            </span>
          </label>
        </div>
        <label className="field" style={{ marginTop: 8 }}>
          <span>Or add it to another book you have</span>
          <select
            className="select"
            value={choice.action === "join" && !candidateIds.has(choice.work_id) ? String(choice.work_id) : other}
            onChange={(e) => {
              setOther(e.target.value);
              if (e.target.value) setChoice({ action: "join", work_id: Number(e.target.value) });
            }}
          >
            <option value="">Choose a book…</option>
            {[...works].sort((a, b) => a.sort_title.localeCompare(b.sort_title)).map((w) => (
              <option key={w.id} value={w.id}>{w.title} — {w.author}{w.audio ? " (audio)" : ""}</option>
            ))}
          </select>
        </label>
        {choice.action === "join" && tracks.length > 0 && (targetHasAudio || needsAppend) && (
          <label className="check warn-check">
            <input type="checkbox" checked={append} onChange={(e) => setAppend(e.target.checked)} />
            “{target?.title}” already has an audiobook. Add these {tracks.length} {tracks.length === 1 ? "file" : "tracks"} after its last track.
          </label>
        )}
      </fieldset>

      {editing && meta ? (
        <div className="import-edit">
          <MetadataForm value={meta} onChange={setMeta} errors={errors} detected={imp.detected} />
        </div>
      ) : (
        <button className="btn sm ghost" onClick={() => setEditing(true)} aria-expanded={editing}>
          <Pencil /> Edit details{choice.action === "join" ? " (fills empty fields only)" : ""}
        </button>
      )}

      <details className="import-files">
        <summary>{imp.files.length} {imp.files.length === 1 ? "file" : "files"}</summary>
        <ul>
          {imp.files.map((f) => (
            <li key={f.id}>
              <span className="name" title={f.filename}>{f.filename}</span>
              <span className="hint">{f.converted_from ? `Converted from ${f.converted_from} to EPUB · ` : ""}{bytes(f.size)}{f.duration_s ? ` · ${clock(f.duration_s)}` : ""}{f.status === "duplicate" ? " · duplicate" : ""}</span>
              {imp.files.length > 1 && (
                <button className="icon-btn ghost sm" aria-label={`Remove ${f.filename} from this book`} title="Remove" onClick={() => removeFile(f.id)}>
                  <Close />
                </button>
              )}
            </li>
          ))}
        </ul>
      </details>

      {siblings.length > 0 && (
        <div className="row combine-row">
          <span className="hint">Same book uploaded separately (an EPUB and its audiobook)?</span>
          <select className="select sm" aria-label="Combine with another upload" value="" onChange={(e) => e.target.value && combine(e.target.value)}>
            <option value="">Combine with…</option>
            {siblings.map((s) => <option key={s.id} value={s.id}>{s.detected?.title || s.label}</option>)}
          </select>
        </div>
      )}

      {err && <div className="alert error" role="alert">{err}</div>}
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button className="btn ghost" onClick={discard} disabled={busy}><Close /> Discard</button>
        <button className="btn primary" onClick={file} disabled={busy || (choice.action === "join" && tracks.length > 0 && needsAppend && !append)} aria-busy={busy}>
          {busy ? "Filing…" : choice.action === "join" ? "Add to book" : "File as new book"}
        </button>
      </div>
    </div>
  );
}
