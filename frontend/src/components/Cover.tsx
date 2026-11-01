import type { CSSProperties } from "react";
import type { Work } from "../lib/types";

export function tintStyle(work: Pick<Work, "cover_tint">): CSSProperties {
  return work.cover_tint ? ({ "--tint": work.cover_tint } as CSSProperties) : {};
}

/** Covers are shown card-sized: the server's cached 360 px thumbnail (not the full art). */
export function thumbUrl(url: string): string {
  return /^\/library\/works\/\d+\/cover\.jpg(\?|$)/.test(url) ? `${url}${url.includes("?") ? "&" : "?"}w=360` : url;
}

export function Cover({ work, className = "" }: { work: Pick<Work, "title" | "author" | "cover_url" | "cover_tint">; className?: string }) {
  return (
    <div className={`cover ${className}`} style={tintStyle(work)}>
      {work.cover_url ? (
        // Not draggable on its own: dragging a book in the Library drags the book (lib/folders),
        // and a dragged image would look like a file upload to the drop overlay.
        <img src={thumbUrl(work.cover_url)} alt="" loading="lazy" decoding="async" draggable={false} />
      ) : (
        <div className="fallback">
          <strong>{work.title}</strong>
          <span>{work.author}</span>
        </div>
      )}
    </div>
  );
}
