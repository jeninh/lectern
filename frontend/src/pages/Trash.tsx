import { useCallback, useEffect, useState } from "react";
import { EmptyState } from "../components/EmptyState";
import { Book, Headphones, Trash as TrashIcon, Undo } from "../components/Icons";
import { PageTitle } from "../components/Shell";
import { del, get, post } from "../lib/api";
import { absoluteTime, bytes, relativeTime } from "../lib/format";
import { useStore } from "../lib/store";
import type { TrashList } from "../lib/types";

/** "deleted in 30 days" right after trashing: whole days remaining, rounded up. */
function daysLeft(purgeAt: string | null, now: number): string {
  if (!purgeAt) return "";
  const days = Math.ceil((Date.parse(purgeAt) - now) / 86_400_000);
  return days <= 1 ? "deleted within a day" : `deleted in ${days} days`;
}

/** Trashed books and removed files: recoverable for 30 days, then purged in the background. */
export function TrashPage() {
  const { toast, refreshLibrary, now } = useStore();
  const [data, setData] = useState<TrashList | null>(null);
  const load = useCallback(() => get<TrashList>("/api/trash").then(setData), []);
  useEffect(() => {
    load().catch((e) => toast((e as Error).message, "error"));
  }, [load, toast]);

  const act = async (fn: () => Promise<unknown>, msg: string) => {
    try {
      await fn();
      toast(msg);
      await Promise.all([load(), refreshLibrary()]);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  const total = (data?.works.length ?? 0) + (data?.files.length ?? 0);
  return (
    <>
      <PageTitle sub={data ? `Kept for ${data.retention_days} days, then deleted for good` : undefined}>Trash</PageTitle>
      {!data ? (
        <div className="hint" aria-busy="true">Loading…</div>
      ) : total === 0 ? (
        <EmptyState icon={<TrashIcon />} title="Trash is empty">
          Books and files you remove wait here for {data.retention_days} days so you can change your mind.
        </EmptyState>
      ) : (
        <div className="stack">
          {data.works.length > 0 && (
            <section className="card" aria-labelledby="tw-h">
              <h2 id="tw-h">Books</h2>
              <ul className="trash-list">
                {data.works.map((t) => (
                  <li key={t.work.id}>
                    <span className="trash-icon" aria-hidden="true"><Book /></span>
                    <div className="titles">
                      <strong>{t.work.title}</strong>
                      <span className="hint">
                        {t.work.author} · trashed {relativeTime(t.trashed_at, now)}{t.trashed_by ? ` by @${t.trashed_by}` : ""} ·{" "}
                        <span title={absoluteTime(t.purge_at)}>{daysLeft(t.purge_at, now)}</span>
                      </span>
                    </div>
                    <div className="row">
                      <button className="btn sm" onClick={() => act(() => post(`/api/trash/works/${t.work.id}/restore`), `Restored “${t.work.title}”.`)}>
                        <Undo /> Restore
                      </button>
                      {data.can_purge && (
                        <button
                          className="btn sm danger"
                          onClick={() => window.confirm(`Delete “${t.work.title}” and its files for good? This cannot be undone.`) && act(() => del(`/api/trash/works/${t.work.id}`), `Deleted “${t.work.title}”.`)}
                        >
                          Delete now
                        </button>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          )}
          {data.files.length > 0 && (
            <section className="card" aria-labelledby="tf-h">
              <h2 id="tf-h">Removed files</h2>
              <p className="sub">EPUBs and audio tracks removed from a book. Restoring puts them back on that book.</p>
              <ul className="trash-list">
                {data.files.map((f) => (
                  <li key={f.id}>
                    <span className="trash-icon" aria-hidden="true">{f.kind === "audio" ? <Headphones /> : <Book />}</span>
                    <div className="titles">
                      <strong>{f.name}</strong>
                      <span className="hint">
                        {bytes(f.size)} · from {f.work ? `“${f.work.title}”${f.work.in_trash ? " (in the trash)" : ""}` : "a deleted book"} · removed {relativeTime(f.trashed_at, now)}
                        {f.trashed_by ? ` by @${f.trashed_by}` : ""}
                      </span>
                    </div>
                    <div className="row">
                      {f.work && !f.work.in_trash && (
                        <button className="btn sm" onClick={() => act(() => post(`/api/trash/files/${f.id}/restore`), `Restored ${f.name}.`)}>
                          <Undo /> Restore
                        </button>
                      )}
                      {data.can_purge && (
                        <button className="btn sm danger" onClick={() => window.confirm(`Delete ${f.name} for good?`) && act(() => del(`/api/trash/files/${f.id}`), `Deleted ${f.name}.`)}>
                          Delete now
                        </button>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          )}
          {!data.can_purge && <p className="hint">Only the owner can delete things before the {data.retention_days} days are up.</p>}
        </div>
      )}
    </>
  );
}
