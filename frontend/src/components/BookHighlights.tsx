import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { get } from "../lib/api";
import { annotationPath, annotationWhere, byChapter } from "../lib/annotations";
import type { Annotation, Work, WorkAnnotations } from "../lib/types";
import { Download } from "./Icons";

/**
 * A book's KOReader highlights and notes (docs/koreader-plugin.md#highlights-and-notes), in
 * reading order under their chapters, the same as the Markdown export. `onPick` (the reader)
 * goes to a highlight in place; otherwise each one links there. Renders nothing while there
 * are none, unless `empty` says what to show instead.
 */
export function BookHighlights({ work, onPick, empty, heading = true }: { work: Pick<Work, "id" | "files" | "title">; onPick?: (a: Annotation) => void; empty?: React.ReactNode; heading?: boolean }) {
  const [data, setData] = useState<WorkAnnotations | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    get<WorkAnnotations>(`/api/works/${work.id}/annotations`)
      .then((d) => live && setData(d))
      .catch((e) => live && setError((e as Error).message));
    return () => {
      live = false;
    };
  }, [work.id]);

  if (error) return empty ? <p className="hint" role="alert">{error}</p> : null;
  if (!data) return null;
  if (!data.annotations.length) return empty ? <>{empty}</> : null;
  const n = data.annotations.length;
  return (
    <section className="highlights" aria-label={`Highlights in ${work.title}`}>
      <div className="row highlights-head">
        {heading && <h3 className="detail-h">Highlights <span className="hint">{n}</span></h3>}
        <a className="btn sm ghost" href={data.export_url} download aria-label={`Export the highlights of ${work.title} as Markdown`}><Download /> Markdown</a>
      </div>
      {byChapter(data.annotations).map((g, i) => (
        <div key={i} className="highlight-group">
          {g.chapter && <h4>{g.chapter}</h4>}
          <ul>
            {g.items.map((a) => {
              const to = annotationPath(work, a);
              return (
                <li key={a.id} className="highlight" data-color={a.color ?? undefined}>
                  <blockquote>{a.text}</blockquote>
                  {a.note && <p className="highlight-note"><strong>Note:</strong> {a.note}</p>}
                  <div className="hint highlight-meta">
                    <span>{annotationWhere(a)}</span>
                    {to && (onPick ? (
                      <button className="btn sm ghost" onClick={() => onPick(a)}>Go to passage</button>
                    ) : (
                      <Link className="btn sm ghost" to={to}>Open in reader</Link>
                    ))}
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </section>
  );
}
