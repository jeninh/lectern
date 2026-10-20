import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { clock, usePlayer } from "../lib/player";
import { absoluteTime, bytes, percent, relativeTime } from "../lib/format";
import { useStore } from "../lib/store";
import type { Work } from "../lib/types";
import { Cover, tintStyle } from "./Cover";
import { ReadAloudFormats } from "./ReadAloudFormats";
import { Book, Chevron, Download, Headphones, Reader, Sync } from "./Icons";

/** KOReader positions look like "/body/DocFragment[12]/body/p[3]/text().0"; show the chapter. */
function position(p: string): string {
  const m = /DocFragment\[(\d+)\]/.exec(p);
  return m ? `Section ${m[1]}` : p.length > 24 ? `${p.slice(0, 24)}…` : p;
}

export function ContinueCard({ work }: { work: Work }) {
  const { me } = useStore(); // also re-renders on the shared clock tick so relative times stay fresh
  const showTech = !!me?.preferences.show_technical_details;
  const [open, setOpen] = useState(false);
  const player = usePlayer();
  const nav = useNavigate();
  const p = work.progress ?? null;
  const pos = work.position;
  if (!p && !pos) return null;
  // Show whichever record is newer: KOReader's sync or the web reader / player.
  const canonical = !!pos && (!p || work.latest === "canonical");
  const file = work.files.find((f) => f.id === work.progress_file_id) ?? work.files.find((f) => f.kind === "ebook") ?? work.files[0];
  const device = canonical ? pos!.device || (pos!.source === "audio" ? "Audio player" : "Web reader") : p!.device || p!.sync_key_label || "a device";
  const updated = canonical ? pos!.received_at : p!.updated_at;
  const id = `ccard-${work.id}`;
  const synced = work.files.flatMap((f) => (f.progress ? [{ file: f, progress: f.progress }] : []));
  const devices = synced.length ? synced : p && file ? [{ file, progress: p }] : [];
  const pct = Math.round((canonical ? pos!.fraction ?? 0 : p!.percentage) * 100);
  const where = canonical ? (pos!.source === "audio" && pos!.audio_s != null ? `Listening · ${clock(pos!.audio_s)}` : "Web reader") : position(p!.progress);
  const ebooks = work.files.filter((f) => f.kind === "ebook");

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
            <span>{where}</span>
          </span>
          <span className="progress-bar" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label={`${work.title} progress`}>
            <span style={{ width: `${pct}%` }} />
          </span>
          <span className="sync">
            <Sync />
            <span>
              {device} · <time dateTime={updated ?? undefined} title={absoluteTime(updated)}>{relativeTime(updated)}</time>
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
              {ebooks.map((f) => (
                <li key={f.id} className="slot">
                  <Book />
                  <span><strong>EPUB</strong> <span className="hint">{bytes(f.size)}</span></span>
                  <span className="row" style={{ gap: 4 }}>
                    <button className="btn sm" onClick={() => nav(`/app/read/${work.id}`)} aria-label={`Read ${work.title}`}><Reader /> Read</button>
                    <a className="btn sm ghost" href={f.download_url} aria-label={`Download EPUB of ${work.title}`}><Download /></a>
                  </span>
                </li>
              ))}
              {work.audio ? (
                <li className="slot">
                  <Headphones />
                  <span><strong>Audiobook</strong> <span className="hint">{clock(work.audio.duration_s)}</span></span>
                  <button className="btn sm" onClick={() => player.open(work, { autoplay: true })} aria-label={`Listen to ${work.title}`}><Headphones /> Listen</button>
                </li>
              ) : (
                <li className="slot" aria-disabled="true">
                  <Headphones />
                  <span><strong>Audiobook</strong> <span className="hint">None yet. Upload an M4B or MP3s.</span></span>
                </li>
              )}
              <ReadAloudFormats work={work} active={open} />
            </ul>
          </section>
          {devices.length > 0 && <section>
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
          </section>}
          <section className="reading">
            <h4>Reading</h4>
            <dl className="kv">
              <dt>Position</dt>
              <dd>{where} · {pct}%</dd>
              <dt>Last saved</dt>
              <dd>{absoluteTime(updated)}</dd>
              <dt>Device</dt>
              <dd>{device}</dd>
            </dl>
            {showTech && p && (
              <details className="tech">
                <summary>Technical details</summary>
                <dl className="kv">
                  <dt>KOReader position</dt>
                  <dd><code>{p.progress}</code></dd>
                  {file?.partial_md5 && (
                    <>
                      <dt>Document id</dt>
                      <dd><code>{file.partial_md5}</code></dd>
                    </>
                  )}
                  {file && (
                    <>
                      <dt>Stored at</dt>
                      <dd><code>{work.dir_path}/{file.rel_path}</code></dd>
                    </>
                  )}
                </dl>
              </details>
            )}
            <div className="row" style={{ marginTop: 12 }}>
              <Link className="btn sm" to={`/app/library/${work.id}`}>Open in library</Link>
            </div>
          </section>
        </div>
      </div>
    </article>
  );
}
