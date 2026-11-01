import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { moveItem } from "../lib/queues";
import { addWantToRead, nextInSeries, removeWantToRead, reorderWantToRead, wantToRead } from "../lib/readingOrder";
import { useStore } from "../lib/store";
import type { Work } from "../lib/types";
import { Cover, tintStyle } from "./Cover";

function Book({ work, children }: { work: Work; children?: React.ReactNode }) {
  const label = work.series ? `${work.series}${work.series_index != null ? ` #${work.series_index}` : ""}` : work.author;
  return (
    <div>
      <Link className="book" to={`/app/library/${work.id}`} style={tintStyle(work)}>
        <Cover work={work} />
        <strong>{work.title}</strong>
        <span>{label}</span>
      </Link>
      {children}
    </div>
  );
}

/** Automatic: the next unread book of every series you have started. Hidden when empty. */
export function NextInSeriesRow() {
  const [items, setItems] = useState<Work[]>([]);
  useEffect(() => {
    nextInSeries().then(setItems).catch(() => {});
  }, []);
  if (items.length === 0) return null;
  return (
    <section aria-labelledby="next-series-h">
      <div className="section-head">
        <h2 id="next-series-h">Next in series</h2>
        <span className="hint">The next unread book of each series you have started</span>
      </div>
      <div className="shelf">{items.map((w) => <Book key={w.id} work={w} />)}</div>
    </section>
  );
}

/** Manual: only what you added. Reorder with Earlier/Later, remove with Remove. */
export function WantToReadRow() {
  const { toast } = useStore();
  const [items, setItems] = useState<Work[]>([]);
  useEffect(() => {
    wantToRead().then(setItems).catch(() => {});
  }, []);
  const act = useCallback(
    (run: () => Promise<Work[]>) => run().then(setItems).catch((e) => toast((e as Error).message, "error")),
    [toast],
  );
  if (items.length === 0) return null;
  const ids = items.map((w) => w.id);
  return (
    <section aria-labelledby="want-read-h">
      <div className="section-head">
        <h2 id="want-read-h">Want to read</h2>
        <span className="hint">Your own list; add books from their page</span>
      </div>
      <div className="shelf">
        {items.map((w, i) => (
          <Book key={w.id} work={w}>
            <div className="row">
              <button className="btn sm ghost" disabled={i === 0} aria-label={`Move ${w.title} earlier`} onClick={() => act(() => reorderWantToRead(moveItem(ids, i, -1)))}>Earlier</button>
              <button className="btn sm ghost" disabled={i === items.length - 1} aria-label={`Move ${w.title} later`} onClick={() => act(() => reorderWantToRead(moveItem(ids, i, 1)))}>Later</button>
              <button className="btn sm ghost" aria-label={`Remove ${w.title} from Want to read`} onClick={() => act(() => removeWantToRead(w.id))}>Remove</button>
            </div>
          </Book>
        ))}
      </div>
    </section>
  );
}

/** Book-page toggle for the Want to read queue. */
export function WantToReadButton({ workId }: { workId: number }) {
  const { toast } = useStore();
  const [queued, setQueued] = useState<boolean | null>(null);
  useEffect(() => {
    wantToRead().then((x) => setQueued(x.some((w) => w.id === workId))).catch(() => {});
  }, [workId]);
  if (queued === null) return null;
  const toggle = () =>
    (queued ? removeWantToRead(workId) : addWantToRead(workId))
      .then((x) => {
        setQueued(x.some((w) => w.id === workId));
        toast(queued ? "Removed from Want to read." : "Added to Want to read.");
      })
      .catch((e) => toast((e as Error).message, "error"));
  return <button className="btn" onClick={toggle}>{queued ? "Remove from Want to read" : "Want to read"}</button>;
}
