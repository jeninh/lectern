import { useState } from "react";
import { Link } from "react-router-dom";
import { absoluteTime, bytes, percent, relativeTime } from "../lib/format";
import { useStore } from "../lib/store";
import type { Work } from "../lib/types";
import { Cover, tintStyle } from "./Cover";
import { Book, Chevron, Download, Headphones, Reader, Sync } from "./Icons";

/** KOReader positions look like "/body/DocFragment[12]/body/p[3]/text().0"; show the chapter. */
function position(p: string): string {
  const m = /DocFragment\[(\d+)\]/.exec(p);
  return m ? `Section ${m[1]}` : p.length > 24 ? `${p.slice(0, 24)}…` : p;
}

export function ContinueCard({ work }: { work: Work }) {
  useStore(); // re-render on the shared clock tick so relative times stay fresh
  const [open, setOpen] = useState(false);
  const p = work.progress;
  if (!p) return null;
  const file = work.files.find((f) => f.id === work.progress_file_id) ?? work.files[0];
  const device = p.device || p.sync_key_label || "a device";
  const id = `ccard-${work.id}`;
  const synced = work.files.flatMap((f) => (f.progress ? [{ file: f, progress: f.progress }] : []));
  const devices = synced.length ? synced : [{ file, progress: p }];
  const pct = Math.round(p.percentage * 100);

  return (
    <article className="ccard" style={tintStyle(work)} data-open={open || undefined}>
      <button className="ccard-toggle" aria-expanded={open} aria-controls={`${id}-more`} onClick={() => setOpen((o) => !o)}>
        <Cover work={work} />
        <span className="body">
          <span className="title">{work.title}</span>
          <span className="author">{work.author}</span>
          <span className="where">
            <span className="pct">{pct}%</span>
            <span className="sep" aria-hidden="true">·</span>
            <span>{position(p.progress)}</span>
          </span>
          <span className="progress-bar" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label={`${work.title} progress`}>
            <span style={{ width: `${pct}%` }} />
          </span>
          <span className="sync">
            <Sync />
            <span>
              {device} · <time dateTime={p.updated_at ?? undefined} title={absoluteTime(p.updated_at)}>{relativeTime(p.updated_at)}</time>
            </span>
          </span>
        </span>
        <span className="chev" aria-hidden="true"><Chevron width={18} height={18} /></span>
      </button>

      <div className="ccard-more" id={`${id}-more`} role="region" aria-label={`${work.title} details`} {...(open ? {} : { inert: "", "aria-hidden": true })}>
        <div className="ccard-more-inner">
          <section>
            <h4>Formats</h4>
            <ul className="slots">
              {work.files.map((f) => (
                <li key={f.id} className="slot">
                  <Book />
                  <span><strong>EPUB</strong> <span className="hint">{bytes(f.size)}</span></span>
                  <a className="btn sm ghost" href={f.download_url} aria-label={`Download EPUB of ${work.title}`}><Download /> Download</a>
                </li>
              ))}
              <li className="slot" aria-disabled="true">
                <Headphones />
                <span><strong>Audiobook</strong> <span className="hint">Not linked yet</span></span>
                <span className="soon">Soon</span>
              </li>
              <li className="slot" aria-disabled="true">
                <Sync />
                <span><strong>Read-along</strong> <span className="hint">Text + audio in step</span></span>
                <span className="soon">Soon</span>
              </li>
            </ul>
          </section>
          <section>
            <h4>Synced devices</h4>
            <ul className="slots">
              {devices.map(({ file: f, progress: fp }) => (
                <li key={f.id + fp.document} className="slot">
                  <Reader />
                  <span>
                    <strong>{fp.device || fp.sync_key_label || "Unknown device"}</strong>{" "}
                    <span className="hint">{percent(fp.percentage)} · {relativeTime(fp.updated_at)}</span>
                  </span>
                  {fp.sync_key_label && fp.device && fp.sync_key_label !== fp.device && <span className="badge">{fp.sync_key_label}</span>}
                </li>
              ))}
            </ul>
          </section>
          <section>
            <h4>File</h4>
            <dl className="kv">
              <dt>Last sync</dt>
              <dd>{absoluteTime(p.updated_at)}</dd>
              <dt>Position</dt>
              <dd><code>{p.progress}</code></dd>
              {file?.partial_md5 && (
                <>
                  <dt>KOReader id</dt>
                  <dd><code>{file.partial_md5}</code></dd>
                </>
              )}
              {file && (
                <>
                  <dt>Stored at</dt>
                  <dd><code>{file.rel_path}</code></dd>
                </>
              )}
            </dl>
            <div className="row" style={{ marginTop: 12 }}>
              <Link className="btn sm" to={`/app/library/${work.id}`}>Open in library</Link>
            </div>
          </section>
        </div>
      </div>
    </article>
  );
}
