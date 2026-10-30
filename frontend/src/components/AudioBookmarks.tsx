// Audiobook bookmarks and clips with notes (issue #81, lib/audiobookmarks.ts): the one-tap
// bookmark button, the clip editor ("last 30 s" plus handles), and the list shown on the
// Listening page, next to the highlights on the book and in the reader.

import { useCallback, useEffect, useId, useState } from "react";
import { Link } from "react-router-dom";
import { get } from "../lib/api";
import {
  type AudioBookmark,
  clipBounds,
  createMark,
  deleteMark,
  lastSeconds,
  markLabel,
  markReadAlongPath,
  markReaderPath,
  MARKS_CHANGED,
  marksChanged,
  MAX_CLIP_S,
  QUICK_CLIP_S,
  updateMark,
  type WorkAudioBookmarks,
} from "../lib/audiobookmarks";
import { clock, usePlayer } from "../lib/player";
import { useStore } from "../lib/store";
import type { Work } from "../lib/types";
import { Headphones, Pause, Play } from "./Icons";

/** A book's marks, refetched when one is made or changed anywhere on the page. */
export function useAudioMarks(workId: number | null | undefined) {
  const [data, setData] = useState<WorkAudioBookmarks | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    if (!workId) return;
    get<WorkAudioBookmarks>(`/api/works/${workId}/audio-bookmarks`)
      .then((d) => (setData(d), setError(null)))
      .catch((e) => setError((e as Error).message));
  }, [workId]);
  useEffect(() => {
    setData(null);
    load();
    const on = (e: Event) => (e as CustomEvent<{ workId: number }>).detail.workId === workId && load();
    window.addEventListener(MARKS_CHANGED, on);
    return () => window.removeEventListener(MARKS_CHANGED, on);
  }, [workId, load]);
  return { data, error, reload: load };
}

/** One tap: a bookmark where the player is now, in the chapter it shows. `onMade`: what the
 * toast's "Add a note" does (playback and the keyboard are left alone until then). */
export function BookmarkButton({ className = "btn sm ghost", onMade, label = true }: { className?: string; onMade?: (m: AudioBookmark) => void; label?: boolean }) {
  const p = usePlayer();
  const { toast, deviceName } = useStore();
  const [busy, setBusy] = useState(false);
  if (!p.work) return null;
  const w = p.work;
  const make = async () => {
    setBusy(true);
    try {
      const m = await createMark(w.id, { start_s: Math.round(p.t * 100) / 100, chapter: p.chapter?.title ?? null, device: deviceName });
      marksChanged(w.id);
      toast(`Bookmarked ${clock(m.start_s)}.`, "info", onMade ? { label: "Add a note", run: () => onMade(m) } : undefined);
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <button className={className} onClick={make} disabled={busy} aria-label={`Bookmark this moment, ${clock(p.t)}`} title="Bookmark this moment (B)" aria-keyshortcuts="B">
      <BookmarkIcon /> {label && <span>Bookmark</span>}
    </button>
  );
}

export function BookmarkIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M6 3h12v18l-6-4-6 4z" />
    </svg>
  );
}

/** The clip being made: starts as the last 30 s, then each handle can be moved. */
export function ClipEditor({ onDone }: { onDone: () => void }) {
  const p = usePlayer();
  const { toast, deviceName } = useStore();
  const id = useId();
  const w = p.work;
  const [range, setRange] = useState(() => lastSeconds(p.t));
  // The handles move within a window around the clip, so a second is a usable step.
  const [win] = useState(() => ({ from: Math.max(0, range.start - 120), to: Math.min(p.duration || range.end, range.end + 60) }));
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  if (!w) return null;
  const move = (which: "start" | "end", v: number) =>
    setRange((r) => clipBounds(which === "start" ? v : r.start, which === "end" ? v : r.end, p.duration, which));
  const previewing = p.clip?.workId === w.id && p.clip.start === range.start && p.playing;
  const save = async () => {
    setBusy(true);
    try {
      await createMark(w.id, { start_s: range.start, end_s: range.end, note: note.trim() || null, chapter: p.chapter?.title ?? null, device: deviceName });
      marksChanged(w.id);
      toast(`Clip saved: ${clock(range.start)}–${clock(range.end)}.`);
      onDone();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="clip-editor" aria-labelledby={`${id}-h`}>
      <h3 id={`${id}-h`} className="detail-h">New clip <span className="hint tabular">{clock(range.start)}–{clock(range.end)} · {Math.round(range.end - range.start)} s</span></h3>
      <label className="clip-handle">
        <span>Start</span>
        <input type="range" min={win.from} max={win.to} step={0.5} value={range.start} onChange={(e) => move("start", Number(e.target.value))} aria-valuetext={clock(range.start)} />
        <span className="tabular">{clock(range.start)}</span>
      </label>
      <label className="clip-handle">
        <span>End</span>
        <input type="range" min={win.from} max={win.to} step={0.5} value={range.end} onChange={(e) => move("end", Number(e.target.value))} aria-valuetext={clock(range.end)} />
        <span className="tabular">{clock(range.end)}</span>
      </label>
      <label className="sr-only" htmlFor={`${id}-note`}>Note</label>
      <textarea id={`${id}-note`} className="input" rows={2} placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} maxLength={20000} />
      <div className="row clip-actions">
        <button className="btn sm ghost" onClick={() => (previewing ? p.pause() : p.playClip(w, range.start, range.end))}>
          {previewing ? <Pause /> : <Play />} {previewing ? "Stop" : "Play clip"}
        </button>
        <span className="hint">Up to {MAX_CLIP_S / 60} minutes; times only, played from the audiobook itself.</span>
        <button className="btn sm ghost" onClick={onDone}>Cancel</button>
        <button className="btn sm primary" onClick={save} disabled={busy}>Save clip</button>
      </div>
    </section>
  );
}

/** "Bookmark" and "Clip last 30 s" for the full player, the clip editor when open, and this
 * book's marks below. */
export function PlayerMarks() {
  const p = usePlayer();
  const [clipping, setClipping] = useState(false);
  const [editing, setEditing] = useState<number | null>(null);
  const { data } = useAudioMarks(p.work?.id);
  // B: bookmark the moment (when nothing else has the keyboard).
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if (e.key !== "b" || e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.isContentEditable || ["INPUT", "SELECT", "TEXTAREA"].includes(t.tagName))) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      e.preventDefault();
      document.querySelector<HTMLButtonElement>("[data-bookmark-now]")?.click();
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);
  if (!p.work) return null;
  const w = p.work;
  return (
    <>
      <span className="row np-marks">
        <span data-bookmark-now="" style={{ display: "contents" }}>
          <BookmarkButton onMade={(m) => setEditing(m.id)} />
        </span>
        <button className="btn sm ghost" onClick={() => setClipping(true)} aria-expanded={clipping} title={`Clip the last ${QUICK_CLIP_S} seconds, then adjust`}>
          <ClipIcon /> Clip last {QUICK_CLIP_S} s
        </button>
      </span>
      {clipping && <ClipEditor onDone={() => setClipping(false)} />}
      {data && data.bookmarks.length > 0 && (
        <section aria-labelledby="np-marks-h" className="audio-marks">
          <div className="row highlights-head">
            <h3 id="np-marks-h" className="detail-h">Bookmarks and clips <span className="hint">{data.bookmarks.length}</span></h3>
          </div>
          <MarkList work={w} marks={data.bookmarks} editing={editing} setEditing={setEditing} />
        </section>
      )}
    </>
  );
}

function ClipIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <path d="M4 12h3M17 12h3M9 7v10M15 7v10" />
    </svg>
  );
}

/** Marks in time order: what they cover, the note (editable), and where they lead. `onText`
 * (the reader) goes to an aligned mark's passage in place. */
export function MarkList({ work, marks, editing, setEditing, onText }: {
  work: Pick<Work, "id" | "files" | "title"> & Partial<Work>;
  marks: AudioBookmark[];
  editing?: number | null;
  setEditing?: (id: number | null) => void;
  onText?: (m: AudioBookmark) => void;
}) {
  const p = usePlayer();
  const { toast } = useStore();
  const [local, setLocal] = useState<number | null>(null);
  const open = editing !== undefined ? editing : local;
  const setOpen = setEditing ?? setLocal;
  const remove = async (m: AudioBookmark) => {
    try {
      await deleteMark(m.id);
      marksChanged(m.work_id);
      toast(`${markLabel(m)} deleted.`);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  const play = (m: AudioBookmark) => {
    const full = p.work?.id === work.id ? p.work : (work as Work);
    if (m.end_s != null) p.playClip(full, m.start_s, m.end_s).catch(() => {});
    else if (p.work?.id === work.id) (p.seek(m.start_s), p.play());
    else p.open(full, { at: m.start_s, autoplay: true }).catch(() => {});
  };
  return (
    <ul className="audio-mark-list">
      {marks.map((m) => {
        const reader = markReaderPath(work, m);
        const along = markReadAlongPath(m);
        const playing = p.clip?.workId === work.id && p.clip.start === m.start_s && p.playing;
        return (
          <li key={m.id} className="highlight audio-mark" data-kind={m.kind}>
            <div className="audio-mark-head">
              <span className="badge audio-mark-badge"><Headphones /> Audio</span>
              <strong className="tabular">{markLabel(m)}</strong>
              {m.chapter && <span className="hint">· {m.chapter}</span>}
            </div>
            {m.text && <blockquote>{m.text}</blockquote>}
            {open === m.id ? (
              <NoteEditor mark={m} onDone={() => setOpen(null)} />
            ) : (
              m.note && <p className="highlight-note"><strong>Note:</strong> {m.note}</p>
            )}
            <div className="hint highlight-meta">
              <span>{new Date(m.created_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}{m.device ? ` · ${m.device}` : ""}</span>
              <span className="row audio-mark-actions">
                <button className="btn sm ghost" onClick={() => (playing ? p.pause() : play(m))} aria-label={`${playing ? "Pause" : "Play"} ${markLabel(m)}`}>
                  {playing ? <Pause /> : <Play />} {playing ? "Pause" : m.end_s != null ? "Play clip" : "Play from here"}
                </button>
                {reader && (onText ? (
                  <button className="btn sm ghost" onClick={() => onText(m)}>Go to passage</button>
                ) : (
                  <Link className="btn sm ghost" to={reader}>Open in reader</Link>
                ))}
                {along && <Link className="btn sm ghost" to={along}>Read along</Link>}
                {open !== m.id && <button className="btn sm ghost" onClick={() => setOpen(m.id)}>{m.note ? "Edit note" : "Add note"}</button>}
                <button className="btn sm ghost" onClick={() => remove(m)} aria-label={`Delete ${markLabel(m)}`}>Delete</button>
              </span>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function NoteEditor({ mark, onDone }: { mark: AudioBookmark; onDone: () => void }) {
  const { toast } = useStore();
  const [note, setNote] = useState(mark.note ?? "");
  const id = useId();
  const save = async () => {
    try {
      await updateMark(mark.id, { note: note.trim() || null });
      marksChanged(mark.work_id);
      onDone();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  return (
    <form className="audio-note" onSubmit={(e) => (e.preventDefault(), save())}>
      <label className="sr-only" htmlFor={id}>Note for {markLabel(mark)}</label>
      <textarea
        id={id}
        className="input"
        rows={2}
        autoFocus
        value={note}
        maxLength={20000}
        placeholder="Note"
        onChange={(e) => setNote(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) (e.preventDefault(), save());
          if (e.key === "Escape") (e.stopPropagation(), onDone());
        }}
      />
      <span className="row">
        <button type="button" className="btn sm ghost" onClick={onDone}>Cancel</button>
        <button type="submit" className="btn sm primary">Save note</button>
      </span>
    </form>
  );
}
