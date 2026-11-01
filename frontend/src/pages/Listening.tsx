import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Cover, tintStyle } from "../components/Cover";
import { PlayerMarks } from "../components/AudioBookmarks";
import { EmptyState } from "../components/EmptyState";
import { Download, Headphones, NextChapter, Play, PrevChapter, Reader, Upload } from "../components/Icons";
import { HandoffPrompt, PlayButton, RateMenu, Scrubber, SkipButtons, SleepMenu } from "../components/Player";
import { PageTitle } from "../components/Shell";
import { get, post } from "../lib/api";
import { clearListening, enqueueListening, listeningQueue, removeListening, reorderListening } from "../lib/listeningQueue";
import { bytes, relativeTime } from "../lib/format";
import { useVisiblePolling } from "../lib/hooks";
import { clock, usePlayer } from "../lib/player";
import { useStore } from "../lib/store";
import { bookFiles, readerPath } from "../lib/formats";
import type { ListeningQueueItem, TracksResponse, Work } from "../lib/types";

export function ListeningPage() {
  const { canUpload, pickBooks, me } = useStore();
  const p = usePlayer();
  const [items, setItems] = useState<Work[] | null>(null);
  const load = useCallback(() => get<{ items: Work[] }>("/api/listening").then((d) => setItems(d.items)), []);
  useEffect(() => {
    load().catch(() => setItems([]));
  }, [load]);

  // Nothing loaded yet: queue up the most recent audiobook (paused) so the page has a player.
  useEffect(() => {
    if (!p.work && items?.length && items[0].position) p.open(items[0]).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);

  const others = (items ?? []).filter((w) => w.id !== p.work?.id);
  return (
    <>
      <PageTitle sub={items ? `${items.length} ${items.length === 1 ? "audiobook" : "audiobooks"}` : undefined}>Listening</PageTitle>
      {items === null ? (
        <div className="skeleton-row" aria-busy="true" aria-label="Loading"><span /><span /></div>
      ) : items.length === 0 ? (
        <EmptyState
          icon={<Headphones />}
          title="No audiobooks yet"
          action={canUpload ? <button className="btn primary" onClick={pickBooks}><Upload /> Upload an audiobook</button> : undefined}
        >
          Upload an M4B or a folder of MP3s, or drop them into <code>{me?.inbox_path ?? "the inbox"}</code>. Lectern files the audio next to the matching EPUB so reading and listening share one place.
        </EmptyState>
      ) : (
        <div className="stack" style={{ gap: 28 }}>
          {p.work && <NowPlaying onChanged={load} />}
          <UpNext />
          {others.length > 0 && (
            <section aria-labelledby="ab-h">
              <div className="section-head"><h2 id="ab-h">{p.work ? "More audiobooks" : "Audiobooks"}</h2></div>
              <ul className="audio-grid">
                {others.map((w) => {
                  const total = w.audio?.duration_s ?? 0;
                  const at = w.position?.audio_s ?? null;
                  const frac = at != null && total ? at / total : null;
                  return (
                    <li key={w.id}>
                      <button className="audio-card" style={tintStyle(w)} onClick={() => p.open(w, { autoplay: true })} aria-label={`Play ${w.title} by ${w.author}`}>
                        <span className="cover-wrap"><Cover work={w} /><span className="play-badge" aria-hidden="true"><Play /></span></span>
                        <span className="titles">
                          <strong>{w.title}</strong>
                          <span>{w.author}</span>
                          <span className="hint">
                            {frac != null ? `${clock(total - (at ?? 0))} left · ${relativeTime(w.position?.received_at)}` : clock(total)}
                          </span>
                          {frac != null && <span className="progress-bar"><span style={{ width: `${Math.round(frac * 100)}%` }} /></span>}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </section>
          )}
        </div>
      )}
    </>
  );
}

function UpNext() {
  const p = usePlayer();
  const { toast } = useStore();
  const [items, setItems] = useState<ListeningQueueItem[]>([]);
  const load = useCallback(() => listeningQueue().then((x) => setItems(x.items)), []);
  useEffect(() => {
    load().catch(() => {});
    const changed = () => load().catch(() => {});
    window.addEventListener("lectern:listening-queue", changed);
    return () => window.removeEventListener("lectern:listening-queue", changed);
  }, [load]);
  const act = async (run: () => Promise<unknown>) => {
    try { await run(); await load(); } catch (e) { toast((e as Error).message, "error"); }
  };
  return (
    <section className="card" aria-labelledby="up-next-h">
      <div className="section-head"><div><h2 id="up-next-h">Up next</h2><p className="hint">Playback queue, separate from your reading lists.</p></div>{items.length > 0 && <button className="btn sm ghost" onClick={() => act(clearListening)}>Clear</button>}</div>
      <label className="row" style={{ marginBottom: 10 }}><input type="checkbox" checked={p.stopAfterCurrent} onChange={(e) => p.setStopAfterCurrent(e.target.checked)} /> Stop after current item <span className="hint">This session only</span></label>
      {items.length === 0 ? <p className="hint">Nothing queued. Add an audiobook from the Library.</p> : <ol className="slots">
        {items.map((item, i) => <li key={item.id} className="slot"><span className="n tabular">{i + 1}</span><span><strong>{item.work.title}</strong> <span className="hint">· {item.work.author}{item.track_position != null ? ` · chapter ${item.track_position + 1}` : ""}</span></span><button className="btn sm ghost" disabled={!i} onClick={() => act(() => reorderListening(items.map((x, n) => n === i ? items[i - 1].id : n === i - 1 ? item.id : x.id)))}>Up</button><button className="btn sm ghost" onClick={() => act(() => removeListening(item.id))}>Remove</button></li>)}
      </ol>}
    </section>
  );
}

const CHAPTER_WINDOW = 8;

function NowPlaying({ onChanged }: { onChanged: () => void }) {
  const p = usePlayer();
  const { canUpload, toast, me } = useStore();
  const showTech = !!me?.preferences.show_technical_details;
  const w = p.work!;
  const [info, setInfo] = useState<TracksResponse | null>(null);
  const refresh = useCallback(() => get<TracksResponse>(`/api/works/${w.id}/tracks`).then(setInfo), [w.id]);
  useEffect(() => {
    refresh().catch(() => {});
  }, [refresh]);
  const busy = info?.transcode && ["queued", "leased"].includes(info.transcode.status);
  // Copies turned off, or nothing that could make one: offer no button that would fail.
  const canMake = canUpload && !info?.data_saver?.off && !info?.data_saver?.unavailable;
  const accountSaver = !!me?.preferences.data_saver;
  useVisiblePolling(() => busy && refresh().catch(() => {}), 4000);

  const lowReady = !!info?.tracks.length && info.tracks.every((t) => t.variants.low);
  const lowSize = info?.tracks.reduce((s, t) => s + (t.variants.low?.size ?? 0), 0) ?? 0;
  const origSize = info?.tracks.reduce((s, t) => s + t.size, 0) ?? 0;
  const ebook = bookFiles(w).length > 0;

  // The chapter list flows in the page (no scroll box of its own, which would trap scrolling);
  // a long one shows a few chapters around the current one until "Show all". The window moves
  // only when the current chapter leaves it, and never while focus is in the list, so a focused
  // row is not unmounted under the keyboard.
  const [allChapters, setAllChapters] = useState(false);
  const [windowStart, setWindowStart] = useState(0);
  const [focusInList, setFocusInList] = useState(false);
  const long = p.chapters.length > CHAPTER_WINDOW + 2;
  const windowed = long && !allChapters;
  const at = p.chapter ? p.chapters.indexOf(p.chapter) : 0;
  useEffect(() => {
    setAllChapters(false);
    setWindowStart(0);
  }, [w.id]);
  const from = windowed ? Math.max(0, Math.min(windowStart, p.chapters.length - CHAPTER_WINDOW)) : 0;
  useEffect(() => {
    if (windowed && !focusInList && (at < from || at >= from + CHAPTER_WINDOW)) setWindowStart(Math.max(0, at - 2));
  }, [windowed, focusInList, at, from]);
  const shownChapters = windowed ? p.chapters.slice(from, from + CHAPTER_WINDOW) : p.chapters;

  const transcode = async () => {
    try {
      const r = await post<{ status: string }>(`/api/works/${w.id}/transcode`);
      toast(r.status === "ready" ? "The data-saver copy is ready." : "Making a data-saver copy. It is kept for next time.");
      await refresh();
      onChanged();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  return (
    <section className="now-playing" style={tintStyle(w)} aria-labelledby="np-title">
      <div className="np-main">
        <Cover work={w} />
        <div className="np-body">
          <div className="np-kicker">Now playing</div>
          <h2 id="np-title">{w.title}</h2>
          <div className="hint">{w.author}{p.chapter ? ` · ${p.chapter.title}` : ""}</div>
          <HandoffPrompt />
          <Scrubber />
          <div className="np-controls">
            <button className="icon-btn ghost" onClick={p.prevChapter} aria-label="Previous chapter" title="Previous chapter"><PrevChapter /></button>
            <SkipButtons><PlayButton big /></SkipButtons>
            <button className="icon-btn ghost" onClick={p.nextChapter} aria-label="Next chapter" title="Next chapter"><NextChapter /></button>
          </div>
          <div className="np-options">
            <RateMenu />
            <SleepMenu />
            <span className="np-saver">
              <button
                className="switch"
                role="switch"
                aria-checked={p.dataSaver}
                aria-labelledby="saver-l"
                aria-describedby="saver-d"
                onClick={() => p.setDataSaver(!p.dataSaver)}
              >
                <span />
              </button>
              <span id="saver-l">Data saver</span>
              {p.dataSaverDevice !== null && p.dataSaverDevice !== accountSaver && (
                <button className="linklike hint" onClick={() => p.setDataSaver(null)} title="Use your account's setting (Security) on this device">Use account setting</button>
              )}
            </span>
            {ebook && <Link className="btn sm ghost" to={readerPath(w)}><Reader /> Read</Link>}
            <button className="btn sm ghost" onClick={() => enqueueListening(w.id).then(() => toast("Added to Up next.")).catch((e) => toast((e as Error).message, "error"))}>Add to Up next</button>
          </div>
          <p className="hint" id="saver-d">
            {p.dataSaver
              ? p.usingLow
                ? `Streaming the ${info?.tracks[0]?.variants.low?.bitrate_kbps ?? 64} kbps copy (${bytes(lowSize)} instead of ${bytes(origSize)}).`
                : lowReady
                  ? "Data saver is on."
                  : busy
                    ? info?.transcode?.message ?? `Making the data-saver copy… ${info?.transcode?.progress ?? 0} of ${info?.transcode?.total ?? 0} files done.`
                    : "No data-saver copy yet; streaming the original."
              : "Streams the original files. Data saver uses a smaller copy for slow connections such as phones away from home."}
            {p.dataSaver && !lowReady && !busy && canMake && <> <button className="linklike" onClick={transcode}>Make one now</button>.</>}
            {p.dataSaver && !lowReady && !busy && canUpload && info?.data_saver?.unavailable && <> {info.data_saver.unavailable.message}</>}
          </p>
          <PlayerMarks />
        </div>
      </div>

      <div className="np-cols">
        <section aria-labelledby="ch-h">
          <h3 id="ch-h">Chapters</h3>
          <ol
            className="chapters"
            id="np-chapters"
            start={from + 1}
            aria-label={windowed ? `Chapters ${from + 1}–${from + shownChapters.length} of ${p.chapters.length}` : undefined}
            onFocus={() => setFocusInList(true)}
            onBlur={(e) => setFocusInList(e.currentTarget.contains(e.relatedTarget as Node | null))}
          >
            {shownChapters.map((c, j) => {
              const i = from + j;
              const current = p.chapter === c;
              return (
                <li key={i}>
                  <button aria-current={current ? "true" : undefined} onClick={() => p.seek(c.start)}>
                    <span className="n tabular">{i + 1}</span>
                    <span className="t">{c.title}</span>
                    <span className="d tabular">{clock(c.end - c.start)}</span>
                  </button>
                  <button className="btn sm ghost" onClick={() => {
                    const track = p.tracks.find((t) => t.offset_s <= c.start && t.offset_s + t.duration_s > c.start);
                    if (track) enqueueListening(w.id, track.id, c.start - track.offset_s, c.end - track.offset_s).catch((e) => toast((e as Error).message, "error"));
                  }} aria-label={`Add ${c.title} to Up next`}>Add</button>
                </li>
              );
            })}
          </ol>
          {long && (
            <button className="linklike" onClick={() => setAllChapters(!allChapters)} aria-expanded={allChapters} aria-controls="np-chapters" style={{ marginTop: 8 }}>
              {allChapters ? "Show fewer chapters" : `Show all ${p.chapters.length} chapters`}
            </button>
          )}
        </section>
        <section aria-labelledby="files-h">
          <h3 id="files-h">Files</h3>
          <ul className="slots">
            {(info?.tracks ?? p.tracks).map((t) => (
              <li key={t.id} className="slot">
                <Headphones />
                <span>
                  <strong>{(info?.tracks.length ?? 1) > 1 ? `Part ${t.position + 1}` : "Audiobook"}</strong>{" "}
                  <span className="hint">{t.codec.toUpperCase()} · {clock(t.duration_s)} · {bytes(t.size)}</span>
                  {showTech && <span className="hint mono"> · {t.sha256.slice(0, 12)}</span>}
                </span>
                <a className="btn sm ghost" href={t.download_url} aria-label={`Download original ${t.position + 1}`}><Download /> Original</a>
              </li>
            ))}
          </ul>
          {canMake && !lowReady && !busy && (
            <button className="btn sm" onClick={transcode} style={{ marginTop: 10 }}>Make a data-saver copy</button>
          )}
          {lowReady && <p className="hint">Data-saver copy ready: {bytes(lowSize)} instead of {bytes(origSize)}{origSize > lowSize ? ` (${bytes(origSize - lowSize)} saved)` : ""}. Originals stay untouched.</p>}
        </section>
      </div>
    </section>
  );
}
