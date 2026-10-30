import { useEffect, useState } from "react";
import { del, get, post, put } from "../lib/api";
import { type DraftRead, dayLabel, localToday, notesTokens, readsBody, starFill, starsLabel, toDraft } from "../lib/reviews";
import { useStore } from "../lib/store";
import type { BookReview as Review } from "../lib/types";
import { DocMarkdown } from "./DocMarkdown";

const NO_FILES: ReadonlySet<string> = new Set();
const HALVES = [0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5];

/**
 * Pick a rating, ½ to 5 stars: each star is two targets, its left half and its right half.
 * Choosing the current rating again clears it.
 */
export function Stars({ value, onRate, label = "Your rating", disabled = false }: { value: number | null; onRate: (v: number | null) => void; label?: string; disabled?: boolean }) {
  return (
    <span className="stars" role="radiogroup" aria-label={label}>
      {[1, 2, 3, 4, 5].map((i) => (
        <span key={i} className="star" data-fill={starFill(value, i)}>
          <span className="star-glyph" aria-hidden="true">★</span>
          {HALVES.filter((v) => Math.ceil(v) === i).map((v) => (
            <button
              key={v}
              type="button"
              role="radio"
              aria-checked={value === v}
              aria-label={starsLabel(v)}
              title={value === v ? "Clear rating" : starsLabel(v)}
              className={`star-hit ${v === i ? "right" : "left"}`}
              disabled={disabled}
              onClick={() => onRate(value === v ? null : v)}
            />
          ))}
        </span>
      ))}
    </span>
  );
}

function useReview(workId: number) {
  const [review, setReview] = useState<Review | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setReview(null);
    get<Review>(`/api/works/${workId}/review`)
      .then((r) => live && setReview(r))
      .catch((e) => live && setError((e as Error).message));
    return () => {
      live = false;
    };
  }, [workId]);
  return { review, setReview, error };
}

/**
 * "Rate it?" for a book just finished (the reader, at the end): the stars and "Not now".
 * Shows nothing once the book is rated or the prompt was dismissed.
 */
export function RatePrompt({ workId, className = "review-prompt" }: { workId: number; className?: string }) {
  const { toast } = useStore();
  const { review, setReview } = useReview(workId);
  const [done, setDone] = useState(false);
  if (!review || done || review.rating !== null || review.dismissed) return null;
  const act = async (fn: () => Promise<Review>, after?: string) => {
    try {
      setReview(await fn());
      setDone(true);
      if (after) toast(after);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  return (
    <div className={className} role="status" aria-label="Rate this book">
      <span><strong>Finished!</strong> Rate it? <span className="hint">Only you see your rating.</span></span>
      <span className="row">
        <Stars value={null} onRate={(v) => v && act(() => put<Review>(`/api/works/${workId}/review`, { rating: v }), `Rated ${starsLabel(v)}`)} />
        <button className="btn sm ghost" onClick={() => act(() => post<Review>(`/api/works/${workId}/review/dismiss`))}>Not now</button>
      </span>
    </div>
  );
}

/**
 * Your private rating, notes (Markdown), and read-throughs of a book, on its detail page
 * (docs/reviews.md). Nobody else sees them. Until you save dates, the one read-through shown
 * comes from your reading progress; saving any replaces it.
 */
export function BookReview({ work }: { work: { id: number; title: string } }) {
  const { toast } = useStore();
  const { review, setReview, error } = useReview(work.id);
  const [editing, setEditing] = useState(false);
  const [notes, setNotes] = useState("");
  const [reads, setReads] = useState<DraftRead[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (error) return <p className="hint" role="alert">{error}</p>;
  if (!review) return null;
  const url = `/api/works/${work.id}/review`;

  const send = async (fn: () => Promise<Review>) => {
    setBusy(true);
    try {
      setReview(await fn());
      return true;
    } catch (e) {
      toast((e as Error).message, "error");
      return false;
    } finally {
      setBusy(false);
    }
  };
  const rate = (v: number | null) => send(() => put<Review>(url, { rating: v }));
  const edit = () => {
    setNotes(review.notes);
    setReads(review.read_throughs.map(toDraft));
    setProblem(null);
    setEditing(true);
  };
  const save = async () => {
    const body = readsBody(reads, localToday());
    if (typeof body === "string") {
      setProblem(body);
      return;
    }
    // Derived dates left as they were stay derived (they keep following your reading).
    const before = JSON.stringify(review.read_throughs.map(toDraft));
    const changed = JSON.stringify(reads) !== before;
    if (await send(() => put<Review>(url, { notes, ...(changed ? { read_throughs: body } : {}) }))) setEditing(false);
  };
  const forget = () => send(() => del<Review>(url)).then((ok) => ok && setEditing(false));
  const setRead = (i: number, patch: Partial<DraftRead>) => setReads(reads.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const today = localToday();

  return (
    <section className="book-review" aria-label={`Your rating and notes for ${work.title}`}>
      <div className="row review-head">
        <h3 className="detail-h">Your rating and notes <span className="hint">only you see these</span></h3>
        {!editing && <button className="btn sm ghost" onClick={edit}>{review.notes || review.corrected ? "Edit" : "Add notes or dates"}</button>}
      </div>
      {review.prompt && (
        <div className="review-prompt" role="status">
          <span><strong>You finished this book.</strong> Rate it?</span>
          <button className="btn sm ghost" disabled={busy} onClick={() => send(() => post<Review>(`${url}/dismiss`))}>Not now</button>
        </div>
      )}
      <div className="row review-rating">
        <Stars value={review.rating} onRate={rate} disabled={busy} />
        <span className="hint">{starsLabel(review.rating)}</span>
      </div>

      {editing ? (
        <form className="stack review-edit" onSubmit={(e) => { e.preventDefault(); void save(); }}>
          <fieldset className="review-reads">
            <legend>Read</legend>
            {reads.length === 0 && <p className="hint">No dates yet. Add one for a book you read before Lectern.</p>}
            {reads.map((r, i) => (
              <div key={i} className="row review-read">
                <label className="field">
                  <span>Started</span>
                  <input className="input sm" type="date" max={today} value={r.started_on} onChange={(e) => setRead(i, { started_on: e.target.value })} />
                </label>
                <label className="field">
                  <span>Finished</span>
                  <input className="input sm" type="date" max={today} value={r.finished_on} onChange={(e) => setRead(i, { finished_on: e.target.value })} />
                </label>
                <button type="button" className="btn sm ghost" aria-label={`Remove read ${i + 1}`} onClick={() => setReads(reads.filter((_, j) => j !== i))}>Remove</button>
              </div>
            ))}
            <button type="button" className="btn sm ghost" onClick={() => setReads([...reads, { started_on: "", finished_on: "" }])}>{reads.length ? "Add a re-read" : "Add a read"}</button>
          </fieldset>
          <label className="field">
            <span>Notes <span className="hint">Markdown</span></span>
            <textarea className="input" rows={6} value={notes} maxLength={20000} onChange={(e) => setNotes(e.target.value)} placeholder="What you thought of it. Only you see this." />
          </label>
          {problem && <p className="field-error" role="alert">{problem}</p>}
          <div className="row review-actions">
            <button type="button" className="btn sm ghost danger" disabled={busy} onClick={forget}>Forget all</button>
            <span className="row">
              <button type="button" className="btn sm ghost" onClick={() => setEditing(false)}>Cancel</button>
              <button className="btn sm primary" disabled={busy}>Save</button>
            </span>
          </div>
        </form>
      ) : (
        <>
          {review.read_throughs.length > 0 && (
            <ul className="review-dates">
              {review.read_throughs.map((r, i) => (
                <li key={r.id ?? `d${i}`}>
                  {r.finished_on ? (r.started_on ? `Read ${dayLabel(r.started_on)} – ${dayLabel(r.finished_on)}` : `Finished ${dayLabel(r.finished_on)}`) : `Reading since ${dayLabel(r.started_on)}`}
                  {r.derived && <span className="hint"> · from your reading progress</span>}
                </li>
              ))}
            </ul>
          )}
          {review.notes ? (
            <div className="review-notes doc-body">
              <DocMarkdown tokens={notesTokens(review.notes)} files={NO_FILES} />
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
