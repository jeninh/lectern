import type { CSSProperties } from "react";
import type { Work } from "../lib/types";

export function tintStyle(work: Pick<Work, "cover_tint">): CSSProperties {
  return work.cover_tint ? ({ "--tint": work.cover_tint } as CSSProperties) : {};
}

export function Cover({ work, className = "" }: { work: Work; className?: string }) {
  return (
    <div className={`cover ${className}`} style={tintStyle(work)}>
      {work.cover_url ? (
        <img src={work.cover_url} alt="" loading="lazy" decoding="async" />
      ) : (
        <div className="fallback">
          <strong>{work.title}</strong>
          <span>{work.author}</span>
        </div>
      )}
    </div>
  );
}
