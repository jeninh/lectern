import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, del, get, patch, post, postBlob } from "../lib/api";
import { buildPatch, changes, coverAuthor, initialDraft, rebase, type Draft } from "../lib/editor";
import { bytes, relativeTime } from "../lib/format";
import { applyFields, display, FIELD_LABEL, suggestionDiff, validate } from "../lib/metadata";
import { clock } from "../lib/player";
import { useStore } from "../lib/store";
import type { CoverStyle, EditorState, LookupResult, LookupSuggestion, Metadata, TracksResponse } from "../lib/types";
import { Book, Down, Headphones, Shuffle, Sparkle, Trash, Undo, Up, Upload } from "./Icons";
import { MetadataForm } from "./MetadataForm";
import { Sheet } from "./Sheet";

type Tab = "details" | "cover" | "files" | "playback" | "shelves";
const TABS: { id: Tab; label: string }[] = [
  { id: "details", label: "Details" },
  { id: "cover", label: "Cover" },
  { id: "files", label: "Files & tracks" },
  { id: "playback", label: "Playback" },
  { id: "shelves", label: "Shelves" },
];
const uuid = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;

/** The full work editor. One Save sends one versioned PATCH; the toast after it offers Undo. */
export function WorkEditor() {
  const { editWorkId, openEditor, toast, refreshLibrary } = useStore();
  const [base, setBase] = useState<EditorState | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [tab, setTab] = useState<Tab>("details");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [confirmClose, setConfirmClose] = useState(false);
  const [conflict, setConflict] = useState<EditorState | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const idem = useRef(uuid());
  const nav = useNavigate();

  useEffect(() => {
    setBase(null);
    setDraft(null);
    setTab("details");
    setConflict(null);
    setConfirmClose(false);
    setLoadError(null);
    setSaveError(null);
    if (editWorkId == null) return;
    get<EditorState>(`/api/works/${editWorkId}/edit`)
      .then((s) => {
        setBase(s);
        setDraft(initialDraft(s));
      })
      .catch((e) => setLoadError((e as Error).message));
  }, [editWorkId]);

  const pending = useMemo(() => (base && draft ? changes(base, draft) : []), [base, draft]);
  const dirty = pending.length > 0;
  const errors = useMemo(() => (draft ? validate(draft.metadata) : {}), [draft]);

  // Closing the tab or reloading with unsaved edits asks first.
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  // The discard prompt takes focus so Enter or Space never discards by accident.
  useEffect(() => {
    if (confirmClose) document.querySelector<HTMLButtonElement>(".save-bar.confirm .btn")?.focus();
  }, [confirmClose]);

  const close = useCallback(() => {
    openEditor(null);
  }, [openEditor]);
  const requestClose = () => (dirty ? setConfirmClose(true) : close());

  const undo = useCallback(
    async (workId: number, revisionId: number, title: string) => {
      try {
        const fresh = await post<EditorState>(`/api/works/${workId}/undo`, { revision_id: revisionId });
        toast(`Undid the last save of “${title}”.`);
        setBase((b) => {
          if (b?.work.id !== workId) return b;
          setDraft(initialDraft(fresh));
          return fresh;
        });
        await refreshLibrary();
      } catch (e) {
        toast((e as Error).message, "error");
      }
    },
    [toast, refreshLibrary],
  );

  const save = async () => {
    if (!base || !draft) return;
    if (Object.keys(errors).length) {
      setTab("details");
      setSaveError("Fix the highlighted fields first.");
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const body = buildPatch(base, draft);
      const r = await patch<{ revision_id: number | null; changed: string[]; folder: { moved?: boolean; pending?: boolean; dir_path?: string }; editor: EditorState }>(
        `/api/works/${base.work.id}`,
        body,
        { "idempotency-key": idem.current },
      );
      idem.current = uuid();
      setBase(r.editor);
      setDraft(initialDraft(r.editor));
      setConflict(null);
      const title = r.editor.metadata.title;
      const wid = base.work.id;
      toast(
        r.folder?.pending ? `Saved “${title}”. Its folder is renamed once the read-aloud build finishes.` : `Saved “${title}”.`,
        "info",
        r.revision_id ? { label: "Undo", run: () => undo(wid, r.revision_id!, title) } : undefined,
      );
      refreshLibrary().catch(() => {});
    } catch (e) {
      if (e instanceof ApiError && e.code === "version_conflict" && e.data?.current) {
        setConflict(e.data.current as EditorState);
      } else {
        if (!(e instanceof TypeError)) idem.current = uuid(); // a network retry reuses the key
        setSaveError((e as Error).message);
      }
    } finally {
      setSaving(false);
    }
  };

  const trashWork = async () => {
    if (!base) return;
    if (!window.confirm(`Move “${base.metadata.title}” to the trash? It stays recoverable for 30 days; reading positions are kept.`)) return;
    try {
      const r = await post<{ revision_id: number }>(`/api/works/${base.work.id}/trash`, { version: base.version });
      const title = base.metadata.title;
      const wid = base.work.id;
      openEditor(null);
      nav("/app/library");
      toast(`Moved “${title}” to the trash.`, "info", { label: "Undo", run: () => undo(wid, r.revision_id, title) });
      await refreshLibrary();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => (d ? { ...d, [k]: v } : d));

  const onTabKey = (e: ReactKeyboardEvent) => {
    const i = TABS.findIndex((t) => t.id === tab);
    const next = e.key === "ArrowRight" ? (i + 1) % TABS.length : e.key === "ArrowLeft" ? (i - 1 + TABS.length) % TABS.length : e.key === "Home" ? 0 : e.key === "End" ? TABS.length - 1 : -1;
    if (next < 0) return;
    e.preventDefault();
    setTab(TABS[next].id);
    document.getElementById(`etab-${TABS[next].id}`)?.focus();
  };

  const footer = !base || !draft ? undefined : confirmClose ? (
    <div className="save-bar confirm" role="alertdialog" aria-labelledby="discard-q">
      <span id="discard-q"><strong>Discard unsaved changes?</strong> <span className="hint">{pending.join(", ")}</span></span>
      <div className="row">
        <button className="btn" onClick={() => setConfirmClose(false)}>Keep editing</button>
        <button className="btn danger" onClick={close}>Discard</button>
      </div>
    </div>
  ) : conflict ? (
    <div className="save-bar conflict" role="alert">
      <span>
        <strong>{conflict.last_revision?.actor ? `@${conflict.last_revision.actor}` : "Someone"} saved this book while you were editing</strong>
        <span className="hint"> ({conflict.last_revision?.changed.join(", ") || "other changes"}). Your edits are still here.</span>
      </span>
      <div className="row">
        <button className="btn" onClick={() => { setBase(conflict); setDraft(initialDraft(conflict)); setConflict(null); }}>Use their version</button>
        <button className="btn primary" onClick={() => { setDraft(rebase(base, conflict, draft)); setBase(conflict); setConflict(null); }}>Keep my edits on top</button>
      </div>
    </div>
  ) : (
    <div className="save-bar">
      <span className="hint" aria-live="polite">
        {saveError ? <span className="field-error">{saveError}</span> : dirty ? `Unsaved: ${pending.join(", ")}` : base.last_revision?.undoable ? (
          <>Saved {relativeTime(base.last_revision.created_at)}{base.last_revision.actor ? ` by @${base.last_revision.actor}` : ""}. <button className="linkish" onClick={() => undo(base.work.id, base.last_revision!.id, base.metadata.title)}><Undo width={13} height={13} /> Undo</button></>
        ) : "No changes"}
      </span>
      <div className="row">
        <button className="btn ghost" onClick={requestClose}>{dirty ? "Cancel" : "Close"}</button>
        <button className="btn primary" onClick={save} disabled={!dirty || saving} aria-busy={saving}>{saving ? "Saving…" : "Save"}</button>
      </div>
    </div>
  );

  return (
    <Sheet open={editWorkId != null} onClose={requestClose} title={base ? `Edit “${base.metadata.title}”` : "Edit book"} labelledBy="editor-title" wide footer={footer}>
      {loadError ? (
        <div className="alert error">{loadError}</div>
      ) : !base || !draft ? (
        <div className="hint" aria-busy="true">Loading…</div>
      ) : (
        <div className="editor" {...((confirmClose || conflict) ? { inert: "" } : {})}>
          <div className="tabs" role="tablist" aria-label="Edit sections" onKeyDown={onTabKey}>
            {TABS.map((t) => (
              <button key={t.id} id={`etab-${t.id}`} role="tab" aria-selected={tab === t.id} aria-controls={`epanel-${t.id}`} tabIndex={tab === t.id ? 0 : -1} onClick={() => setTab(t.id)}>
                {t.label}
                {t.id === "details" && Object.keys(errors).length > 0 && <span className="dot danger" aria-label="has errors" />}
              </button>
            ))}
          </div>
          <div role="tabpanel" id={`epanel-${tab}`} aria-labelledby={`etab-${tab}`} className="tab-panel" key={tab}>
            {tab === "details" && (
              <>
                <LookupPanel state={base} metadata={draft.metadata} onApply={(m) => set("metadata", m)} />
                <MetadataForm value={draft.metadata} onChange={(m) => set("metadata", m)} errors={errors} />
              </>
            )}
            {tab === "cover" && <CoverTab state={base} draft={draft} set={set} />}
            {tab === "files" && <FilesTab state={base} draft={draft} set={set} onTrash={trashWork} />}
            {tab === "playback" && <PlaybackTab state={base} draft={draft} set={set} onRefresh={(fresh) => setBase((b) => (b ? { ...b, alignment: fresh.alignment, read_aloud: fresh.read_aloud } : b))} />}
            {tab === "shelves" && <ShelvesTab state={base} draft={draft} set={set} />}
          </div>
        </div>
      )}
    </Sheet>
  );
}

type SetDraft = <K extends keyof Draft>(k: K, v: Draft[K]) => void;

// --- metadata suggestions ------------------------------------------------------------------------

function LookupPanel({ state, metadata, onApply }: { state: EditorState; metadata: Metadata; onApply: (m: Metadata) => void }) {
  const { me } = useStore();
  const [result, setResult] = useState<LookupResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  if (state.lookup_offline && state.lookup_enabled) {
    return (
      <p className="hint lookup-off">
        <Sparkle width={14} height={14} />
        <span>
          Metadata suggestions are unavailable: Lectern is in offline mode and makes no internet connections.
          {me?.user.role === "owner" && <> See <Link to="/app/admin#offline-mode">Admin → Offline mode</Link>.</>}
        </span>
      </p>
    );
  }
  if (!state.lookup_enabled) {
    return (
      <p className="hint lookup-off">
        <Sparkle width={14} height={14} /> Metadata suggestions from Open Library and Google Books are off, because they send the title or ISBN to those services.
        {me?.user.role === "owner" ? <> Turn them on in <Link to="/app/admin#library">Admin</Link>.</> : " The owner can turn them on."}
      </p>
    );
  }
  const run = async () => {
    setLoading(true);
    setError(null);
    setOpen(true);
    try {
      const q = metadata.isbn ? { isbn: metadata.isbn } : { title: metadata.title, author: metadata.authors[0]?.name || undefined };
      setResult(await post<LookupResult>("/api/metadata/lookup", q));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };
  return (
    <section className="lookup" aria-labelledby="lookup-h">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h3 id="lookup-h" className="detail-h" style={{ margin: 0 }}>Suggestions</h3>
        <button className="btn sm" onClick={run} disabled={loading || (!metadata.isbn && !metadata.title.trim())} aria-busy={loading}>
          <Sparkle /> {loading ? "Asking…" : result ? "Ask again" : metadata.isbn ? "Look up this ISBN" : "Look up title and author"}
        </button>
      </div>
      <p className="hint" style={{ margin: "4px 0 0" }}>
        Sends {metadata.isbn ? "only the ISBN" : "the title and first author"} to Open Library and Google Books. Nothing changes until you pick fields and save.
      </p>
      {open && error && <div className="alert error" role="alert">{error}</div>}
      {open && result && (
        <div className="stack" style={{ gap: 12, marginTop: 10 }}>
          {result.errors.map((e) => (
            <div key={e.provider} className="alert warn">{e.provider === "openlibrary" ? "Open Library" : "Google Books"}: {e.error === "timeout" ? "did not answer in time" : e.error === "rate_limited" ? "too many requests, try again in a minute" : e.error.replace(/_/g, " ")}</div>
          ))}
          {result.suggestions.length === 0 && !result.errors.length && <p className="hint">No matches found.</p>}
          {result.suggestions.map((s, i) => (
            <SuggestionTable key={s.provider + i} s={s} current={metadata} onApply={onApply} />
          ))}
        </div>
      )}
    </section>
  );
}

function SuggestionTable({ s, current, onApply }: { s: LookupSuggestion; current: Metadata; onApply: (m: Metadata) => void }) {
  const diff = suggestionDiff(current, s.fields);
  const [chosen, setChosen] = useState<Set<keyof Metadata>>(new Set());
  const toggle = (k: keyof Metadata) => setChosen((c) => {
    const n = new Set(c);
    if (n.has(k)) n.delete(k);
    else n.add(k);
    return n;
  });
  return (
    <div className="suggestion card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <strong>{s.provider_name}</strong>
        <span className="hint">
          <a href={s.source_url} target="_blank" rel="noopener noreferrer nofollow">View source</a> · fetched {relativeTime(s.fetched_at)}
        </span>
      </div>
      {diff.length === 0 ? (
        <p className="hint">Matches what you have.</p>
      ) : (
        <>
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
              {diff.map((k) => (
                <tr key={k} data-chosen={chosen.has(k) || undefined}>
                  <td><input type="checkbox" aria-label={`Use suggested ${FIELD_LABEL[k].toLowerCase()}`} checked={chosen.has(k)} onChange={() => toggle(k)} /></td>
                  <th scope="row">{FIELD_LABEL[k]}</th>
                  <td className="was">{clip(display(k, current[k]))}</td>
                  <td className="now">{clip(display(k, s.fields[k]))}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <button className="btn sm ghost" onClick={() => setChosen(new Set(diff))}>Select all</button>
            <button className="btn sm primary" disabled={!chosen.size} onClick={() => { onApply(applyFields(current, s.fields, [...chosen])); setChosen(new Set()); }}>
              Use {chosen.size || ""} selected {chosen.size === 1 ? "field" : "fields"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

const clip = (t: string) => (t.length > 240 ? t.slice(0, 240) + "…" : t);

// --- cover ---------------------------------------------------------------------------------------------

const COVER_STYLES: [CoverStyle, string][] = [["classic", "Classic"], ["modern", "Modern"], ["minimal", "Minimal"]];

/** The generated cover for the draft title and author, drawn by the server (debounced) so the
 * preview is the exact image the library, OPDS, and KOReader will get. */
function useGeneratedPreview(workId: number, title: string, author: string, style: CoverStyle | null, seed: number, on: boolean) {
  const [shown, setShown] = useState<{ url: string; style: CoverStyle; palette: string } | null>(null);
  useEffect(() => {
    if (!on) return;
    const ctl = new AbortController();
    const q = new URLSearchParams({ title: title.trim().slice(0, 512), author: author.slice(0, 512), seed: String(seed) });
    if (style) q.set("style", style);
    const t = window.setTimeout(async () => {
      try {
        const res = await fetch(`/api/works/${workId}/cover/generated.jpg?${q}`, { credentials: "same-origin", signal: ctl.signal });
        if (!res.ok) return;
        const url = URL.createObjectURL(await res.blob());
        setShown((old) => {
          if (old) URL.revokeObjectURL(old.url);
          return { url, style: (res.headers.get("x-cover-style") as CoverStyle) || "classic", palette: res.headers.get("x-cover-palette") || "" };
        });
      } catch {
        /* aborted by a newer draft, or offline: keep the last preview */
      }
    }, 220);
    return () => (window.clearTimeout(t), ctl.abort());
  }, [workId, title, author, style, seed, on]);
  useEffect(() => () => setShown((old) => (old && URL.revokeObjectURL(old.url), null)), []);
  return shown;
}

function CoverTab({ state, draft, set }: { state: EditorState; draft: Draft; set: SetDraft }) {
  const { toast } = useStore();
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const c = draft.cover;
  const gen = state.cover.generated;
  // Without art (now, or after this save) the generated cover is what everyone sees.
  const generated = c?.action === "generate" || c?.action === "remove" || (!c && !state.cover.has_cover);
  const style = c?.action === "generate" ? c.style ?? null : gen.style;
  const seed = c?.action === "generate" ? c.seed ?? 0 : gen.seed;
  const preview = useGeneratedPreview(state.work.id, draft.metadata.title || state.metadata.title, coverAuthor(draft.metadata.authors), style, seed, generated);
  const shownStyle = style ?? preview?.style ?? gen.effective_style;
  const src = c?.action === "upload" ? c.preview : generated ? preview?.url ?? gen.url : c ? null : state.cover.url;
  const generate = (next: { style?: CoverStyle | null; seed?: number }) => set("cover", { action: "generate", style, seed, ...next });
  const upload = async (f: File) => {
    if (f.size > 10 * 1024 * 1024) return toast("That image is too large for a cover (10 MB at most).", "error");
    setBusy(true);
    try {
      const r = await postBlob<{ token: string; preview_url: string }>(`/api/works/${state.work.id}/cover`, f);
      set("cover", { action: "upload", token: r.token, preview: r.preview_url });
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  const source = state.cover.source === "upload" ? "uploaded" : state.cover.source === "embedded" ? "from the book file" : state.cover.source;
  return (
    <div className="cover-tab">
      <div className="cover big" data-generated={generated || undefined}>
        {src ? <img src={src} alt={generated ? `Generated cover preview, ${shownStyle} style` : "Cover preview"} /> : (
          <div className="fallback">
            <strong>{draft.metadata.title}</strong>
            <span>{c?.action === "embedded" ? "The book’s own cover" : coverAuthor(draft.metadata.authors)}</span>
          </div>
        )}
      </div>
      <div className="stack" style={{ gap: 10 }}>
        <p className="hint" style={{ margin: 0 }} aria-live="polite">
          {c?.action === "upload"
            ? "New image, applied when you save."
            : c?.action === "embedded"
              ? "Uses the cover inside the EPUB (or the first page of a PDF or comic) when you save."
              : generated && state.cover.has_cover
                ? "The generated cover replaces the current image when you save; Undo in the history brings the image back."
                : generated
                  ? `Generated from the title and author${c ? ", applied when you save" : ""}. An uploaded image or the book’s own cover always takes its place.`
                  : `Current cover${source ? ` (${source})` : ""}.`}
          {" "}The book file itself is never changed.
        </p>
        {generated ? (
          <div className="gen-controls">
            <div className="seg" role="radiogroup" aria-label="Cover style">
              {COVER_STYLES.map(([v, l]) => (
                <button key={v} type="button" role="radio" aria-checked={shownStyle === v} aria-pressed={shownStyle === v} onClick={() => generate({ style: v })}>{l}</button>
              ))}
            </div>
            <button type="button" className="btn ghost" onClick={() => generate({ seed: seed + 1 })} title="Try the next colour palette">
              <Shuffle /> Shuffle colours
            </button>
            {preview?.palette && <span className="hint palette-name">{preview.palette}</span>}
          </div>
        ) : (
          <button className="btn ghost" onClick={() => generate({})}><Sparkle /> Generate a cover</button>
        )}
        <input ref={input} type="file" accept="image/jpeg,image/png,image/webp" hidden onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) upload(f); }} />
        <button className="btn" onClick={() => input.current?.click()} disabled={busy} aria-busy={busy}><Upload /> {busy ? "Uploading…" : "Upload an image"}</button>
        {state.cover.embedded_available && <button className="btn ghost" onClick={() => set("cover", { action: "embedded" })} aria-pressed={c?.action === "embedded"}>Use the book’s own cover</button>}
        {c && <button className="btn ghost" onClick={() => set("cover", null)}><Undo /> {state.cover.has_cover ? "Keep the current image" : "Reset"}</button>}
        <p className="hint" style={{ margin: 0 }}>JPEG, PNG, or WebP up to 10 MB.</p>
      </div>
    </div>
  );
}

// --- files and tracks ------------------------------------------------------------------------------------

function FilesTab({ state, draft, set, onTrash }: { state: EditorState; draft: Draft; set: SetDraft; onTrash: () => void }) {
  const { me } = useStore();
  const showTech = !!me?.preferences.show_technical_details;
  const toggle = (k: "remove_file_ids" | "remove_track_ids", id: number) => set(k, draft[k].includes(id) ? draft[k].filter((x) => x !== id) : [...draft[k], id]);
  const move = (i: number, d: number) => {
    const t = [...draft.tracks];
    const j = i + d;
    if (j < 0 || j >= t.length) return;
    [t[i], t[j]] = [t[j], t[i]];
    set("tracks", t);
    requestAnimationFrame(() => document.getElementById(`trk-${t[j].id}-${d < 0 ? "up" : "down"}`)?.focus());
  };
  const keptEpubs = state.files.filter((f) => !draft.remove_file_ids.includes(f.id));
  const keptTracks = draft.tracks.filter((t) => !draft.remove_track_ids.includes(t.id));
  const removingAll = keptEpubs.length === 0 && keptTracks.length === 0;
  return (
    <div className="stack">
      <section aria-labelledby="ebooks-h">
        <h3 id="ebooks-h" className="detail-h"><Book width={14} height={14} /> Books</h3>
        {state.files.length === 0 ? <p className="hint">No EPUB, PDF, or comic. Add one from the Library.</p> : (
          <ul className="file-rows">
            {state.files.map((f) => {
              const removing = draft.remove_file_ids.includes(f.id);
              const epub = f.format === "EPUB";
              return (
                <li key={f.id} data-removing={removing || undefined}>
                  <label className="check">
                    {/* Only an EPUB can be primary: the reader, read-along, and alignment need text. */}
                    {epub ? <input type="radio" name="primary-epub" checked={draft.primary_file_id === f.id} disabled={removing} onChange={() => set("primary_file_id", f.id)} /> : <span className="badge" aria-hidden="true">{f.format}</span>}
                    <span>
                      <strong>{f.rel_path.split("/").pop()}</strong>
                      <span className="hint"> {epub ? "" : `${f.format === "CBZ" ? "Comic" : "PDF"} · `}{bytes(f.size)} · added {relativeTime(f.added_at)}{epub && draft.primary_file_id === f.id ? " · primary" : ""}</span>
                      {showTech && <span className="hint mono"> sha256 {f.sha256.slice(0, 12)}…{f.partial_md5 ? ` · KOReader ${f.partial_md5}` : ""}</span>}
                    </span>
                  </label>
                  <button className="btn sm ghost" onClick={() => toggle("remove_file_ids", f.id)}>{removing ? <><Undo /> Keep</> : <><Trash /> Remove</>}</button>
                </li>
              );
            })}
          </ul>
        )}
        {state.files.filter((f) => f.format === "EPUB").length > 1 && <p className="hint">The primary EPUB is what the reader, read-along, and alignment use.</p>}
        {state.files.some((f) => f.format !== "EPUB") && <p className="hint">PDFs and comics open in the page viewer and sync pages with KOReader. Read-along needs an EPUB edition.</p>}
      </section>

      <section aria-labelledby="tracks-h">
        <h3 id="tracks-h" className="detail-h"><Headphones width={14} height={14} /> Audiobook {draft.tracks.length > 0 && <span className="hint">· {keptTracks.length} {keptTracks.length === 1 ? "track" : "tracks"} · {clock(state.tracks.filter((t) => keptTracks.some((k) => k.id === t.id)).reduce((a, t) => a + t.duration_s, 0))}</span>}</h3>
        {draft.tracks.length === 0 ? <p className="hint">No audiobook.</p> : (
          <ol className="track-rows">
            {draft.tracks.map((t, i) => {
              const info = state.tracks.find((x) => x.id === t.id)!;
              const removing = draft.remove_track_ids.includes(t.id);
              return (
                <li key={t.id} data-removing={removing || undefined}>
                  <div className="track-row">
                    <span className="n" aria-hidden="true">{i + 1}</span>
                    <label className="sr-only" htmlFor={`trk-${t.id}`}>Track {i + 1} title</label>
                    <input id={`trk-${t.id}`} className="input" value={t.title} maxLength={256} disabled={removing} onChange={(e) => set("tracks", draft.tracks.map((x) => (x.id === t.id ? { ...x, title: e.target.value } : x)))} />
                    <span className="hint dur">{clock(info.duration_s)}</span>
                    <button id={`trk-${t.id}-up`} className="icon-btn ghost sm" aria-label={`Move ${t.title} up`} disabled={i === 0} onClick={() => move(i, -1)}><Up /></button>
                    <button id={`trk-${t.id}-down`} className="icon-btn ghost sm" aria-label={`Move ${t.title} down`} disabled={i === draft.tracks.length - 1} onClick={() => move(i, 1)}><Down /></button>
                    <button className="icon-btn ghost sm" aria-label={removing ? `Keep ${t.title}` : `Remove ${t.title}`} title={removing ? "Keep" : "Remove"} onClick={() => toggle("remove_track_ids", t.id)}>{removing ? <Undo /> : <Trash />}</button>
                  </div>
                  {t.chapters.length > 0 && !removing && (
                    <details className="track-chapters">
                      <summary>{t.chapters.length} {t.chapters.length === 1 ? "chapter" : "chapters"}</summary>
                      <ol>
                        {t.chapters.map((c, ci) => (
                          <li key={ci}>
                            <span className="hint mono">{clock(info.chapters[ci]?.start_s ?? 0)}</span>
                            <label className="sr-only" htmlFor={`ch-${t.id}-${ci}`}>Chapter {ci + 1} title</label>
                            <input id={`ch-${t.id}-${ci}`} className="input sm" value={c} maxLength={256} onChange={(e) => set("tracks", draft.tracks.map((x) => (x.id === t.id ? { ...x, chapters: x.chapters.map((cc, k) => (k === ci ? e.target.value : cc)) } : x)))} />
                          </li>
                        ))}
                      </ol>
                    </details>
                  )}
                </li>
              );
            })}
          </ol>
        )}
        {draft.tracks.length > 1 && <p className="hint">Reordering or renaming renames the files on disk; their bytes and checksums stay the same.</p>}
      </section>

      {(draft.remove_file_ids.length > 0 || draft.remove_track_ids.length > 0) && (
        <div className="alert warn" role="note">
          {removingAll ? "Removing every file leaves nothing to read or play. Move the whole book to the trash instead." : "Removed files go to the trash when you save and can be restored for 30 days."}
        </div>
      )}
      <div className="danger-zone">
        <div>
          <strong>Move this book to the trash</strong>
          <p className="hint" style={{ margin: 0 }}>Hidden from everyone at once. Restore it from Trash within 30 days; after that it is deleted.</p>
        </div>
        <button className="btn danger" onClick={onTrash}><Trash /> Move to trash</button>
      </div>
      {showTech && <p className="hint mono">Folder: {state.folder.dir_path}{state.folder.pending ? " (rename pending)" : ""}</p>}
    </div>
  );
}

// --- playback, alignment, read-aloud -----------------------------------------------------------------------

function PlaybackTab({ state, draft, set, onRefresh }: { state: EditorState; draft: Draft; set: SetDraft; onRefresh: (s: EditorState) => void }) {
  const { toast } = useStore();
  const [model, setModel] = useState(state.alignment?.model || state.align_default_model);
  const [busy, setBusy] = useState<"align" | "readaloud" | null>(null);
  const hasEpub = state.files.some((f) => f.format === "EPUB");
  const hasBoth = hasEpub && state.tracks.length > 0;
  const reload = async () => onRefresh(await get<EditorState>(`/api/works/${state.work.id}/edit`));
  const realign = async () => {
    setBusy("align");
    try {
      await post(`/api/works/${state.work.id}/align`, { model, force: true });
      toast(`Alignment queued with the ${model} model.`);
      await reload();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(null);
    }
  };
  const regenerate = async () => {
    setBusy("readaloud");
    try {
      await post(`/api/works/${state.work.id}/read-aloud?force=true`);
      toast("Rebuilding the read-aloud EPUB.");
      await reload();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(null);
    }
  };
  const OPTIONS: { v: Draft["playback_default"]; label: string; hint: string }[] = [
    { v: null, label: "Automatic", hint: "Read-along once aligned" },
    { v: "read", label: "Read", hint: "Open the EPUB reader" },
    { v: "listen", label: "Listen", hint: "Open the audio player" },
    { v: "readalong", label: "Read-along", hint: "Text highlighted as it is read" },
  ];
  return (
    <div className="stack">
      <fieldset className="field">
        <legend>When you open this book</legend>
        <div className="choices compact">
          {OPTIONS.map((o) => (
            <label key={String(o.v)} className="choice" data-checked={draft.playback_default === o.v || undefined}>
              <input type="radio" name="playback" checked={draft.playback_default === o.v} onChange={() => set("playback_default", o.v)} />
              <span className="titles"><strong>{o.label}</strong><span className="hint">{o.hint}</span></span>
            </label>
          ))}
        </div>
      </fieldset>
      <div className="row pref-row">
        <button type="button" role="switch" className="switch" aria-checked={draft.readalong_enabled} aria-labelledby="ra-label" onClick={() => set("readalong_enabled", !draft.readalong_enabled)}>
          <span aria-hidden="true" />
        </button>
        <span id="ra-label"><strong>Read-along</strong><span className="hint"> Offer synchronised text and audio, and the read-aloud EPUB, for this book.</span></span>
      </div>
      <section className="card flat" aria-labelledby="align-h">
        <h3 id="align-h" className="detail-h" style={{ marginTop: 0 }}>Alignment</h3>
        {!hasBoth ? <p className="hint">{hasEpub || !state.files.length ? "Needs both an EPUB and an audiobook." : "Read-along needs an EPUB edition. PDFs and comics have pages, not text, so they cannot be aligned."}</p> : (
          <>
            <p className="hint" style={{ marginTop: 0 }}>
              {state.alignment ? <>Status: <strong>{state.alignment.status}</strong>{state.alignment.model ? ` · ${state.alignment.model} model` : ""}{typeof state.alignment.coverage === "number" ? ` · ${Math.round(state.alignment.coverage * 100)}% of the text matched` : ""}.</> : "Not aligned yet."}
              {" "}Saving text or audio changes re-queues it automatically with the same model.
            </p>
            <div className="row">
              <label className="sr-only" htmlFor="align-model">Model</label>
              <select id="align-model" className="select sm" value={model} onChange={(e) => setModel(e.target.value)}>
                {state.align_models.map((m) => <option key={m} value={m}>{m}{m === state.align_default_model ? " (default)" : ""}</option>)}
              </select>
              <button className="btn sm" onClick={realign} disabled={busy !== null} aria-busy={busy === "align"}>Re-run alignment</button>
            </div>
          </>
        )}
      </section>
      {state.tracks.length > 0 && <DataSaverSection workId={state.work.id} draft={draft} set={set} />}
      <section className="card flat" aria-labelledby="ra-h">
        <h3 id="ra-h" className="detail-h" style={{ marginTop: 0 }}>Read-aloud EPUB</h3>
        <p className="hint" style={{ marginTop: 0 }}>
          {state.read_aloud.message || `Status: ${state.read_aloud.status}`}
          {state.read_aloud.generated_at ? ` · built ${relativeTime(state.read_aloud.generated_at)}` : ""}
        </p>
        <button className="btn sm" onClick={regenerate} disabled={busy !== null || !state.read_aloud.readalong} aria-busy={busy === "readaloud"}>Regenerate now</button>
      </section>
    </div>
  );
}

/** Data-saver copies of this book's audio: the switch (saved with the book), what exists and
 * what it saves, and making or deleting the copies now. */
function DataSaverSection({ workId, draft, set }: { workId: number; draft: Draft; set: SetDraft }) {
  const { toast, me } = useStore();
  const [info, setInfo] = useState<TracksResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => get<TracksResponse>(`/api/works/${workId}/tracks`).then(setInfo, () => {}), [workId]);
  useEffect(() => {
    load();
  }, [load]);
  const ds = info?.data_saver;
  const job = info?.transcode;
  const running = !!job && ["queued", "leased"].includes(job.status);
  const total = info?.tracks.length ?? 0;
  const libraryOff = me?.library_settings?.data_saver_enabled === false;
  const act = async (make: boolean) => {
    setBusy(true);
    try {
      if (make) {
        const r = await post<{ status: string }>(`/api/works/${workId}/transcode`);
        toast(r.status === "ready" ? "The data-saver copy is ready." : "Making a data-saver copy.");
      } else {
        const r = await del<{ deleted_bytes: number }>(`/api/works/${workId}/transcode`);
        toast(`Data-saver copy deleted (${bytes(r.deleted_bytes)} freed). The originals are untouched.`);
      }
      await load();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  const saved = ds ? ds.original_bytes - ds.low_bytes : 0;
  return (
    <section className="card flat" aria-labelledby="ds-h">
      <h3 id="ds-h" className="detail-h" style={{ marginTop: 0 }}>Data saver</h3>
      <div className="row pref-row">
        <button type="button" role="switch" className="switch" aria-checked={draft.data_saver_enabled} aria-labelledby="ds-label" onClick={() => set("data_saver_enabled", !draft.data_saver_enabled)} disabled={libraryOff}>
          <span aria-hidden="true" />
        </button>
        <span id="ds-label"><strong>Make data-saver copies</strong><span className="hint"> {libraryOff ? "Turned off for the whole library in Admin." : "A smaller copy of each audio file for phones and downloads. Turning it off stops new copies; saved ones stay until you delete them."}</span></span>
      </div>
      <p className="hint" id="ds-status">
        {!info ? "…" : running
          ? job!.message ?? `Making the copy… ${job!.progress ?? 0} of ${job!.total} files done.`
          : ds && ds.tracks_ready > 0
            ? `${ds.tracks_ready} of ${total} ${total === 1 ? "file has" : "files have"} a copy: ${bytes(ds.low_bytes)} instead of ${bytes(ds.original_bytes)}${saved > 0 ? ` (${bytes(saved)} saved)` : ""}.`
            : "No data-saver copy yet."}
        {!running && ds?.unavailable && <> {ds.unavailable.message}</>}
      </p>
      <div className="row">
        {(!ds || ds.tracks_ready < total) && !running && (
          <button className="btn sm" onClick={() => act(true)} disabled={busy || !!ds?.off || !!ds?.unavailable} aria-busy={busy}>Make a data-saver copy</button>
        )}
        {((ds?.tracks_ready ?? 0) > 0 || running) && (
          <button className="btn sm ghost" onClick={() => act(false)} disabled={busy}>Delete the copy</button>
        )}
      </div>
    </section>
  );
}

function ShelvesTab({ state, draft, set }: { state: EditorState; draft: Draft; set: SetDraft }) {
  if (!state.shelves.length) return <p className="hint">No shelves you can change. Make one with New shelf in the Library.</p>;
  return (
    <fieldset className="field">
      <legend>Shelves this book is on</legend>
      <div className="chips">
        {state.shelves.map((s) => {
          const on = draft.shelves.includes(s.id);
          return (
            <button key={s.id} type="button" className="chip subtle" aria-pressed={on} onClick={() => set("shelves", on ? draft.shelves.filter((x) => x !== s.id) : [...draft.shelves, s.id])}>
              {on ? "✓ " : "+ "}{(s.path ?? [s.name]).join(" / ")}
            </button>
          );
        })}
      </div>
      <span className="hint">Readers see only books on shelves shared with them.</span>
    </fieldset>
  );
}

