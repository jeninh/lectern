import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { EmptyState } from "../components/EmptyState";
import { Download, Highlighter } from "../components/Icons";
import { PageTitle } from "../components/Shell";
import { get } from "../lib/api";
import type { AnnotationsSummary } from "../lib/types";

/** Your KOReader highlights by book, and all of them as Markdown (docs/koreader-plugin.md).
 * Searching and filtering them is issue #72. */
export function HighlightsPage() {
  const [data, setData] = useState<AnnotationsSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    get<AnnotationsSummary>("/api/annotations").then(setData).catch((e) => setError((e as Error).message));
  }, []);

  return (
    <>
      <PageTitle sub="Notes and highlights from KOReader">Highlights</PageTitle>
      {error && <p className="hint" role="alert">{error}</p>}
      {data && data.total === 0 && (
        <EmptyState icon={<Highlighter />} title="No highlights yet" action={<Link className="btn" to="/app/devices">Set up KOReader</Link>}>
          Passages you highlight in KOReader, and the notes you add to them, show up here and on each book once the Lectern plugin syncs.
        </EmptyState>
      )}
      {data && data.total > 0 && (
        <div className="stack">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span className="hint">{data.total} {data.total === 1 ? "highlight" : "highlights"} in {data.works.length} {data.works.length === 1 ? "book" : "books"}</span>
            <a className="btn sm" href={data.export_url} download><Download /> Export all as Markdown</a>
          </div>
          <table className="list">
            <tbody>
              {data.works.map((w) => (
                <tr key={w.work_id}>
                  <td>
                    <Link to={`/app/library/${w.work_id}`}><strong>{w.title}</strong></Link>
                    <div className="hint">{w.author}</div>
                  </td>
                  <td style={{ textAlign: "right" }} className="hint tabular">{w.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
