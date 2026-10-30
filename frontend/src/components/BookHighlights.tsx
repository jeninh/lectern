import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { get } from "../lib/api";
import { annotationPath, annotationWhere, byChapter } from "../lib/annotations";
import type { Annotation, Work, WorkAnnotations } from "../lib/types";
import { MarkList, useAudioMarks } from "./AudioBookmarks";
import { Download } from "./Icons";

/**
 * A book's KOReader highlights and notes (docs/koreader-plugin.md#highlights-and-notes), in
 * reading order under their chapters, the same as the Markdown export, and after them the
 * audiobook bookmarks and clips (issue #81), marked as audio. `onPick` (the reader) goes to a
 * highlight, or an aligned audio mark's passage, in place; otherwise each one links there.
 * Renders nothing while there are none, unless `empty` says what to show instead.
 */
export function BookHighlights({ work, onPick, empty, heading = true }: { work: Pick<Work, "id" | "files" | "title"> & Partial<Work>; onPick?: (a: Annotation) => void; empty?: React.ReactNode; heading?: boolean }) {
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

  const audio = useAudioMarks(work.id);
  if (error) return empty ? <p className="hint" role="alert">{error}</p> : null;
  if (!data || (!audio.data && !audio.error)) return null;
  const marks = audio.data?.bookmarks ?? [];
  if (!data.annotations.length && !marks.length) return empty ? <>{empty}</> : null;
  const n = data.annotations.length;
  const audioSection = marks.length > 0 && (
    <section className="highlights audio-marks" aria-label={`Audio bookmarks in ${work.title}`}>
      <div className="row highlights-head">
        <h3 className="detail-h">Audio bookmarks <span className="hint">{marks.length}</span></h3>
        {!n && <a className="btn sm ghost" href={data.export_url} download aria-label={`Export the highlights and audio bookmarks of ${work.title} as Markdown`}><Download /> Markdown</a>}
      </div>
      <MarkList
        work={work}
        marks={marks}
        // The reader goes to the passage the way it goes to a highlight's.
        onText={onPick && ((m) => m.location && onPick({ ...AUDIO_AS_HIGHLIGHT, id: -m.id, location: m.location, created: m.created_at, note: m.note, chapter: m.chapter }))}
      />
    </section>
  );
  if (!n) return <>{audioSection}</>;
  return (
    <>
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
    {audioSection}
    </>
  );
}

/** What the reader's `onPick` needs of an aligned audio mark: where its passage is. */
const AUDIO_AS_HIGHLIGHT: Annotation = {
  id: 0, key: "", text: "", note: null, chapter: null, color: null, page: null, pageno: null, created: "", updated: "",
  device: "", file: { id: 0, media_type: "application/epub+zip" }, location: null, sentence_n: null,
};
