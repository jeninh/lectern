import { useState } from "react";
import { Link } from "react-router-dom";
import { absoluteTime, percent, relativeTime } from "../lib/format";
import type { Work } from "../lib/types";
import { Cover, tintStyle } from "./Cover";
import { Chevron, Download, Sync } from "./Icons";

export function ContinueCard({ work }: { work: Work }) {
  const [open, setOpen] = useState(false);
  const p = work.progress;
  if (!p) return null;
  const file = work.files.find((f) => f.id === work.progress_file_id) ?? work.files[0];
  const device = p.device || p.sync_key_label || "a device";
  const id = `ccard-${work.id}`;
  return (
    <article
      className="ccard"
      style={tintStyle(work)}
      aria-expanded={open}
      aria-controls={`${id}-more`}
      onClick={() => !open && setOpen(true)}
      onKeyDown={(e) => {
        if ((e.key === "Enter" || e.key === " ") && e.target === e.currentTarget) {
          e.preventDefault();
          setOpen((o) => !o);
        }
      }}
      tabIndex={0}
      role="button"
      aria-label={`${work.title} by ${work.author}, ${percent(p.percentage)} read, last synced from ${device} ${relativeTime(p.updated_at)}`}
    >
      <Cover work={work} />
      <div className="body">
        <h3>{work.title}</h3>
        <div className="author">{work.author}</div>
        <div className="progress-bar" aria-hidden="true">
          <span style={{ width: `${Math.round(p.percentage * 100)}%` }} />
        </div>
        <div className="sync">
          <Sync />
          <span className="pct">{percent(p.percentage)}</span>
          <span>
            {device} · {relativeTime(p.updated_at)}
          </span>
        </div>
        {open && (
          <div className="expand" id={`${id}-more`} onClick={(e) => e.stopPropagation()}>
            <dl className="kv">
              <dt>Last sync</dt>
              <dd>{absoluteTime(p.updated_at)}</dd>
              <dt>Device</dt>
              <dd>
                {p.device || "—"}
                {p.sync_key_label && <span className="badge accent" style={{ marginLeft: 8 }}>{p.sync_key_label}</span>}
              </dd>
              <dt>Position</dt>
              <dd><code style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{p.progress}</code></dd>
              {file?.partial_md5 && (
                <>
                  <dt>KOReader id</dt>
                  <dd><code style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{file.partial_md5}</code></dd>
                </>
              )}
            </dl>
            <div className="row">
              <Link className="btn sm" to={`/app/library/${work.id}`}>Details</Link>
              {file && (
                <a className="btn sm" href={file.download_url}>
                  <Download /> Download EPUB
                </a>
              )}
              <button className="btn sm ghost" onClick={() => setOpen(false)}>Collapse</button>
            </div>
          </div>
        )}
      </div>
      <span className="chev" aria-hidden="true"><Chevron width={16} height={16} /></span>
    </article>
  );
}
