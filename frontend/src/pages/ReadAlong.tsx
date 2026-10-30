import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { BookmarkIcon, useAudioMarks } from "../components/AudioBookmarks";
import { Cover } from "../components/Cover";
import { EmptyState } from "../components/EmptyState";
import { Alert, Book, Follow, Headphones, NextSentence, Pause, Play, PrevSentence } from "../components/Icons";
import { PageTitle } from "../components/Shell";
import { ApiError, get } from "../lib/api";
import { createMark, markLabel, marksBySentence, marksChanged, startAt } from "../lib/audiobookmarks";
import { getBook, orOffline } from "../lib/offline/store";
import { useLocalStorage, useReducedMotion } from "../lib/hooks";
import { legacyRate, pickRate } from "../lib/playback";
import { readMark, writeMark } from "../lib/player";
import { useStore } from "../lib/store";
import {
  cpIndexer,
  formatClock,
  lastAtOrBefore,
  loadReadAlong,
  locate,
  saveProgress,
  spokenDuration,
  type ReadAlongData,
} from "../lib/readalong";
import type { SentenceInfo, SpineTextResponse, TextBlock, Work } from "../lib/types";

type Mode = "readalong" | "listen";
const SPEEDS = [0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3];
const SAVE_EVERY_MS = 15000;

function isEditable(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  return el.isContentEditable || ["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName);
}

function cpLength(text: string): number {
  return /[\uD800-\uDFFF]/.test(text) ? Array.from(text).length : text.length;
}

function blockTag(kind: TextBlock["kind"]): "h2" | "h3" | "blockquote" | "pre" | "p" {
  if (kind === "h1") return "h2";
  if (kind.startsWith("h")) return "h3";
  if (kind === "quote") return "blockquote";
  if (kind === "pre") return "pre";
  return "p";
}

/** One spine item: blocks of text with every sentence wrapped in a span carrying its number. */
const Chapter = memo(function Chapter({ text, sentences }: { text: SpineTextResponse; sentences: SentenceInfo[] }) {
  let first = 0;
  return (
    <section className="ra-chapter" aria-label={`Part ${text.spine_index + 1}`}>
      {text.blocks.map((b, bi) => {
        const toU16 = cpIndexer(b.text);
        const end = b.start + cpLength(b.text);
        while (first < sentences.length && sentences[first].char_end <= b.start) first++;
        const nodes: ReactNode[] = [];
        let cursor = 0;
        for (let j = first; j < sentences.length && sentences[j].char_start < end; j++) {
          const s = sentences[j];
          const a = toU16(Math.max(s.char_start, b.start) - b.start);
          const z = toU16(Math.min(s.char_end, end) - b.start);
          if (a > cursor) nodes.push(b.text.slice(cursor, a));
          nodes.push(
            <span key={s.n} className="ra-s" data-n={s.n} data-m={s.matched ? "" : undefined}>
              {b.text.slice(a, z)}
            </span>,
          );
          cursor = z;
        }
        if (cursor < b.text.length) nodes.push(b.text.slice(cursor));
        const Tag = blockTag(b.kind);
        return <Tag key={bi} className={`ra-b ra-${b.kind}`}>{nodes}</Tag>;
      })}
    </section>
  );
});

export function ReadAlongPage() {
  const workId = Number(useParams().id);
  const [work, setWork] = useState<Work | null>(null);
  const [data, setData] = useState<ReadAlongData | null>(null);
  const [error, setError] = useState<{ status: number; message: string } | null>(null);

  useEffect(() => {
    let live = true;
    setData(null);
    setError(null);
    Promise.all([orOffline(() => get<Work>(`/api/works/${workId}`), async () => (await getBook(workId))?.work), loadReadAlong(workId)]).then(
      ([w, d]) => live && (setWork(w), setData(d)),
      (e) => live && setError({ status: e instanceof ApiError ? e.status : 0, message: (e as Error).message }),
    );
    return () => {
      live = false;
    };
  }, [workId]);

  if (error) {
    return (
      <>
        <PageTitle>Read-along</PageTitle>
        <EmptyState icon={<Alert />} title={error.status === 404 ? "Book not found" : "Read-along could not load"} action={<Link className="btn" to="/app/library">Back to the library</Link>}>
          {error.status === 404 ? "It may have been removed, or it is not on a shelf shared with you." : error.message}
        </EmptyState>
      </>
    );
  }
  if (!data || !work) {
    return (
      <>
        <PageTitle>Read-along</PageTitle>
        <div className="ra-loading" aria-busy="true" aria-label="Loading read-along">
          {Array.from({ length: 6 }, (_, i) => <div key={i} className="skeleton-line" />)}
        </div>
      </>
    );
  }
  const playable = data.sentences.some((s) => s.matched) && data.tracks.tracks.length > 0;
  if (!playable) {
    return (
      <>
        <PageTitle sub={work.author}>{work.title}</PageTitle>
        <EmptyState icon={<Headphones />} title="Nothing to read along with yet" action={<Link className="btn" to={`/app/library/${work.id}`}>Book details</Link>}>
          {data.tracks.tracks.length === 0
            ? "This book has no audiobook tracks linked to it."
            : "The audiobook has not been aligned with the text yet. Read-along appears once an alignment finishes."}
        </EmptyState>
      </>
    );
  }
  return <Player work={work} data={data} />;
}

function Player({ work, data }: { work: Work; data: ReadAlongData }) {
  const reduced = useReducedMotion();
  const [mode, setMode] = useLocalStorage<Mode>("lectern.readalong.mode", "readalong");
  // Separate speeds: following the text usually wants a gentler pace than listening alone.
  // Listening uses the book's own speed, the one the player uses, kept per book on the server
  // (lib/playback.ts). The read-along speed stays one per browser for every book.
  const { me, toast, deviceName } = useStore();
  const [search] = useSearchParams();
  const [readSpeed, setReadSpeed] = useLocalStorage<number>("lectern.speed.readalong", 1);
  const [listenSpeed, setListenSpeedState] = useState(() => pickRate(data.progress, readMark(work.id), me?.preferences.playback_speed ?? legacyRate() ?? 1));
  const listenSpeedRef = useRef(listenSpeed);
  listenSpeedRef.current = listenSpeed;
  const [follow, setFollow] = useLocalStorage<boolean>("lectern.readalong.follow", true);
  const speed = mode === "readalong" ? readSpeed : listenSpeed;

  const tracks = data.tracks.tracks;
  const total = data.tracks.total_duration_s;
  const sentences = data.sentences;
  const matched = useMemo(() => sentences.filter((s) => s.matched && s.audio_start_s != null), [sentences]);
  const starts = useMemo(() => matched.map((s) => s.audio_start_s as number), [matched]);
  const matchedIndex = useMemo(() => new Map(matched.map((s, i) => [s.n, i])), [matched]);
  const bySpine = useMemo(() => {
    const m = new Map<number, SentenceInfo[]>();
    for (const s of sentences) {
      const list = m.get(s.spine_index) ?? [];
      list.push(s);
      m.set(s.spine_index, list);
    }
    return m;
  }, [sentences]);

  const audio = useRef<HTMLAudioElement>(null);
  const article = useRef<HTMLElement>(null);
  const bar = useRef<HTMLDivElement>(null);
  const spans = useRef(new Map<number, HTMLElement[]>());
  const trackIndex = useRef(0);
  const currentRef = useRef(-1);
  const programmaticUntil = useRef(0);
  const lastSave = useRef({ at: 0, n: -1 });
  const saveTimer = useRef<number | undefined>(undefined);
  const modeRef = useRef(mode);
  modeRef.current = mode;

  const [current, setCurrent] = useState(-1);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [detached, setDetached] = useState(false);
  const [announce, setAnnounce] = useState("");
  const [direction, setDirection] = useState<"up" | "down">("down");

  const now = () => (audio.current ? tracks[trackIndex.current].offset_s + audio.current.currentTime : 0);

  useLayoutEffect(() => {
    const m = new Map<number, HTMLElement[]>();
    article.current?.querySelectorAll<HTMLElement>("[data-n]").forEach((el) => {
      const n = Number(el.dataset.n);
      m.set(n, [...(m.get(n) ?? []), el]);
    });
    spans.current = m;
  }, [data]);

  const sentenceText = (n: number) => (spans.current.get(n) ?? []).map((e) => e.textContent).join(" ");

  const scrollTo = useCallback(
    (n: number, force = false) => {
      const el = spans.current.get(n)?.[0];
      if (!el || modeRef.current !== "readalong") return;
      const r = el.getBoundingClientRect();
      const vh = window.innerHeight;
      const barTop = bar.current?.getBoundingClientRect().top ?? vh;
      if (!force && r.top >= vh * 0.15 && r.bottom <= Math.min(barTop, vh) - vh * 0.12) return;
      programmaticUntil.current = Date.now() + (reduced ? 150 : 1000);
      window.scrollTo({ top: window.scrollY + r.top - vh * 0.3, behavior: reduced ? "auto" : "smooth" });
    },
    [reduced],
  );

  const followRef = useRef({ follow, detached });
  followRef.current = { follow, detached };

  /** Move the highlight. Direct DOM class changes: thousands of spans never re-render. */
  const highlight = useCallback(
    (n: number) => {
      if (n === currentRef.current) return;
      for (const el of spans.current.get(currentRef.current) ?? []) {
        el.classList.remove("is-current");
        el.removeAttribute("aria-current");
      }
      for (const el of spans.current.get(n) ?? []) {
        el.classList.add("is-current");
        el.setAttribute("aria-current", "true");
      }
      currentRef.current = n;
      setCurrent(n);
      if (followRef.current.follow && !followRef.current.detached) scrollTo(n);
    },
    [scrollTo],
  );

  const sentenceAt = useCallback((t: number) => {
    const i = lastAtOrBefore(starts, t);
    return i < 0 ? -1 : matched[i].n;
  }, [starts, matched]);

  const save = useCallback(
    (keepalive = false) => {
      const n = currentRef.current;
      if (n < 0) return;
      lastSave.current = { at: Date.now(), n };
      const listen = modeRef.current === "listen";
      saveProgress(work.id, listen ? "audio" : "web", { sentence_n: n, ...(listen ? { speed: listenSpeedRef.current } : {}) }, keepalive, work.title)
        .then((r) => listen && r?.received_at && writeMark(work.id, { seenAt: r.received_at }))
        .catch(() => {});
    },
    [work.id],
  );

  const setListenSpeed = useCallback(
    (r: number) => {
      listenSpeedRef.current = r;
      setListenSpeedState(r);
      writeMark(work.id, { rate: r });
      save();
    },
    [work.id, save],
  );
  const setSpeed = mode === "readalong" ? setReadSpeed : setListenSpeed;

  const applyRate = useCallback(() => {
    const a = audio.current;
    if (a) {
      a.defaultPlaybackRate = speed;
      a.playbackRate = speed;
    }
  }, [speed]);
  useEffect(applyRate, [applyRate]);

  /** Seek on the work timeline, switching track files when needed. */
  const seek = useCallback(
    (t: number, play?: boolean) => {
      const a = audio.current;
      if (!a) return;
      const { index, local } = locate(tracks, Math.max(0, Math.min(t, total - 0.05)));
      const go = () => {
        a.currentTime = local;
        if (play) a.play().catch(() => {});
      };
      if (index !== trackIndex.current || !a.src) {
        trackIndex.current = index;
        a.src = tracks[index].stream_url;
        applyRate();
        a.addEventListener("loadedmetadata", go, { once: true });
        a.load();
      } else go();
      setTime(t);
      highlight(sentenceAt(t));
    },
    [tracks, total, applyRate, highlight, sentenceAt],
  );

  const seekSentence = useCallback(
    (n: number, opts: { play?: boolean; speak?: boolean } = {}) => {
      const s = sentences[n];
      if (!s || s.audio_start_s == null) return;
      setDetached(false);
      seek(s.audio_start_s + 0.001, opts.play);
      highlight(n);
      scrollTo(n);
      if (opts.speak) setAnnounce(`Sentence ${n + 1} of ${sentences.length}. ${sentenceText(n)}`);
      if (!opts.play) {
        // Stepping through sentences while paused: remember where the reader stopped.
        window.clearTimeout(saveTimer.current);
        saveTimer.current = window.setTimeout(() => save(), 1200);
      }
    },
    [sentences, seek, highlight, scrollTo, save],
  );

  const step = useCallback(
    (delta: number) => {
      const i = currentRef.current >= 0 ? matchedIndex.get(currentRef.current) ?? lastAtOrBefore(starts, now()) : -1;
      const next = matched[Math.max(0, Math.min(matched.length - 1, i + delta))];
      if (next) seekSentence(next.n, { play: !audio.current?.paused, speak: !!audio.current?.paused });
    },
    [matched, matchedIndex, starts, seekSentence],
  );

  const toggle = useCallback(() => {
    const a = audio.current;
    if (!a) return;
    if (a.paused) {
      setDetached(false);
      if (currentRef.current >= 0) scrollTo(currentRef.current);
      if (!a.src) seek(time, true);
      else a.play().catch(() => {});
    } else a.pause();
  }, [seek, time, scrollTo]);

  const changeSpeed = useCallback(
    (dir: 1 | -1) => {
      const i = SPEEDS.indexOf(speed);
      const next = SPEEDS[Math.max(0, Math.min(SPEEDS.length - 1, (i < 0 ? 2 : i) + dir))];
      setSpeed(next);
      setAnnounce(`Speed ${next}×`);
    },
    [speed, setSpeed],
  );

  // Resume from the stored position (no autoplay), or open at an audio bookmark (`?t=`).
  useEffect(() => {
    const p = data.progress;
    const at = startAt(search);
    const n = at != null ? sentenceAt(at) : p.position ? Math.min(p.position.sentence_n, sentences.length - 1) : -1;
    const t = at ?? p.audio_s ?? (n >= 0 ? sentences[n].audio_start_s : null) ?? matched[0].audio_start_s ?? 0;
    const a = audio.current;
    if (!a) return;
    const { index, local } = locate(tracks, t);
    trackIndex.current = index;
    a.src = tracks[index].stream_url;
    a.addEventListener("loadedmetadata", () => (a.currentTime = local), { once: true });
    setTime(t);
    if (n >= 0) {
      highlight(n);
      requestAnimationFrame(() => scrollTo(n, true));
    }
  }, []);

  // Playback loop: follow the audio clock every frame while playing.
  useEffect(() => {
    if (!playing) return;
    let raf = 0;
    let lastUi = 0;
    const tick = (ts: number) => {
      const t = now();
      highlight(sentenceAt(t));
      if (ts - lastUi > 250) {
        lastUi = ts;
        setTime(t);
      }
      if (Date.now() - lastSave.current.at > SAVE_EVERY_MS && lastSave.current.n !== currentRef.current) save();
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing, highlight, sentenceAt, save]);

  const onEnded = () => {
    const a = audio.current;
    if (!a) return;
    if (trackIndex.current < tracks.length - 1) {
      trackIndex.current += 1;
      a.src = tracks[trackIndex.current].stream_url;
      applyRate();
      a.play().catch(() => {});
    } else {
      setPlaying(false);
      save();
    }
  };

  // Manual reading wins: scrolling by hand while following detaches until the reader returns.
  useEffect(() => {
    const userScroll = () => {
      if (!audio.current || audio.current.paused || modeRef.current !== "readalong") return;
      if (followRef.current.follow && !followRef.current.detached) setDetached(true);
    };
    const onScroll = () => {
      const r = spans.current.get(currentRef.current)?.[0]?.getBoundingClientRect();
      if (r) setDirection(r.top < 0 ? "up" : "down");
      if (Date.now() < programmaticUntil.current) return;
      if (!followRef.current.detached) userScroll();
      // Scrolled back to where the narration is: follow again.
      else if (r && r.top > window.innerHeight * 0.15 && r.bottom < window.innerHeight * 0.6) setDetached(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (["PageUp", "PageDown", "Home", "End", "ArrowUp", "ArrowDown"].includes(e.key) && !isEditable(e.target)) userScroll();
    };
    window.addEventListener("wheel", userScroll, { passive: true });
    window.addEventListener("touchmove", userScroll, { passive: true });
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("wheel", userScroll);
      window.removeEventListener("touchmove", userScroll);
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("keydown", onKey);
    };
  }, []);

  // Keyboard shortcuts for the player.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || isEditable(e.target)) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      const onControl = e.target instanceof HTMLElement && !!e.target.closest("button, a");
      if ((e.key === " " && !onControl) || e.key === "k") {
        e.preventDefault();
        toggle();
      } else if (e.key === "ArrowLeft" || e.key === "j") {
        e.preventDefault();
        step(-1);
      } else if (e.key === "ArrowRight" || e.key === "l") {
        e.preventDefault();
        step(1);
      } else if (e.key === "<" || e.key === ">") {
        e.preventDefault();
        changeSpeed(e.key === ">" ? 1 : -1);
      } else if (e.key === "f") {
        e.preventDefault();
        setFollow(!follow);
        setAnnounce(follow ? "Not following the narration" : "Following the narration");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggle, step, changeSpeed, follow, setFollow]);

  // Save on leaving the page or hiding the tab.
  useEffect(() => {
    const onHide = () => document.visibilityState === "hidden" && save(true);
    document.addEventListener("visibilitychange", onHide);
    window.addEventListener("pagehide", onHide);
    return () => {
      document.removeEventListener("visibilitychange", onHide);
      window.removeEventListener("pagehide", onHide);
      save(true);
    };
  }, [save]);

  // Lock-screen and headset controls.
  useEffect(() => {
    const ms = navigator.mediaSession;
    if (!ms) return;
    ms.metadata = new MediaMetadata({
      title: work.title,
      artist: work.author,
      artwork: work.cover_url ? [{ src: new URL(work.cover_url, location.origin).href, sizes: "512x512", type: "image/jpeg" }] : [],
    });
    const handlers: [MediaSessionAction, MediaSessionActionHandler][] = [
      ["play", () => audio.current?.play()],
      ["pause", () => audio.current?.pause()],
      ["previoustrack", () => step(-1)],
      ["nexttrack", () => step(1)],
      ["seekbackward", () => seek(now() - 10, !audio.current?.paused)],
      ["seekforward", () => seek(now() + 10, !audio.current?.paused)],
      ["seekto", (d) => d.seekTime != null && seek(d.seekTime, !audio.current?.paused)],
    ];
    for (const [a, h] of handlers) {
      try {
        ms.setActionHandler(a, h);
      } catch {
        /* unsupported action */
      }
    }
    return () => handlers.forEach(([a]) => { try { ms.setActionHandler(a, null); } catch { /* ignore */ } });
  }, [work, step, seek]);

  // Audiobook bookmarks and clips (issue #81) on the sentences they cover, marked as audio.
  const marks = useAudioMarks(work.id).data?.bookmarks;
  useLayoutEffect(() => {
    const by = marksBySentence(marks ?? []);
    for (const [n, els] of spans.current) {
      const here = by.get(n);
      for (const el of els) {
        el.classList.toggle("has-audio-mark", !!here);
        if (here) el.title = here.map((m) => (m.note ? `${markLabel(m)}: ${m.note}` : markLabel(m))).join("\n");
        else el.removeAttribute("title");
      }
    }
  }, [marks, data]);
  const bookmark = async () => {
    try {
      const t = Math.round(now() * 100) / 100;
      const m = await createMark(work.id, { start_s: t, device: deviceName });
      marksChanged(work.id);
      toast(`Bookmarked ${formatClock(m.start_s)}. Add a note on the book's page.`);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  const onTextClick = (e: React.MouseEvent) => {
    if (window.getSelection()?.toString()) return; // selecting text, not seeking
    const el = (e.target as HTMLElement).closest<HTMLElement>("[data-n][data-m]");
    if (el) seekSentence(Number(el.dataset.n), { play: true });
  };

  const backToNarration = () => {
    setDetached(false);
    if (currentRef.current >= 0) scrollTo(currentRef.current, true);
  };

  const spine = current >= 0 ? sentences[current].spine_index : null;
  const label = mode === "readalong" ? "Read-along speed" : "Listening speed";

  return (
    <div className="ra" data-mode={mode}>
      <PageTitle
        sub={<>{work.author} · <Link to={`/app/library/${work.id}`}>Book details</Link></>}
        aside={
          <div className="seg seg-text" role="group" aria-label="Mode">
            <button aria-pressed={mode === "readalong"} onClick={() => setMode("readalong")}><Book /> Read along</button>
            <button aria-pressed={mode === "listen"} onClick={() => setMode("listen")}><Headphones /> Listen</button>
          </div>
        }
      >
        {work.title}
      </PageTitle>

      {mode === "listen" && (
        <section className="ra-listen" aria-label="Now playing">
          <Cover work={work} />
          <div>
            <p className="ra-listen-title">{work.title}</p>
            <p className="hint">{work.author}</p>
            <p className="hint">
              {current >= 0 ? `Sentence ${current + 1} of ${sentences.length}` : "Ready"}
              {spine != null && ` · Part ${spine + 1}`}
            </p>
          </div>
        </section>
      )}

      <article
        ref={article}
        className="ra-text"
        hidden={mode !== "readalong"}
        aria-label={`${work.title}, text`}
        aria-describedby="ra-help"
        onClick={onTextClick}
      >
        {data.texts.map((t) => (
          <Chapter key={t.spine_index} text={t} sentences={bySpine.get(t.spine_index) ?? []} />
        ))}
      </article>

      {detached && mode === "readalong" && (
        <button className="ra-return" onClick={backToNarration}>
          <span aria-hidden="true">{direction === "up" ? "↑" : "↓"}</span> Back to narration
        </button>
      )}

      <div className="ra-bar" ref={bar} role="region" aria-label="Player">
        <div className="ra-controls">
          <button className="icon-btn ghost" onClick={() => step(-1)} aria-label="Previous sentence" title="Previous sentence (←)">
            <PrevSentence />
          </button>
          <button className="ra-play" onClick={toggle} aria-label={playing ? "Pause" : "Play"} title={`${playing ? "Pause" : "Play"} (Space)`}>
            {playing ? <Pause /> : <Play />}
          </button>
          <button className="icon-btn ghost" onClick={() => step(1)} aria-label="Next sentence" title="Next sentence (→)">
            <NextSentence />
          </button>
        </div>
        <div className="ra-scrub">
          <span className="ra-time" aria-hidden="true">{formatClock(time)}</span>
          <input
            type="range"
            min={0}
            max={total}
            step={0.1}
            value={Math.min(time, total)}
            onChange={(e) => seek(Number(e.target.value), playing)}
            aria-label="Position"
            aria-valuetext={`${spokenDuration(time)} of ${spokenDuration(total)}`}
          />
          <span className="ra-time" aria-hidden="true">{formatClock(total)}</span>
        </div>
        <div className="ra-options">
          <button className="icon-btn ghost" onClick={bookmark} aria-label="Bookmark this moment" title="Bookmark this moment">
            <BookmarkIcon />
          </button>
          <label className="ra-speed" title="Slower (<) / faster (>)">
            <span className="sr-only">{label}</span>
            <span aria-hidden="true" className="ra-speed-label">{mode === "readalong" ? "Read" : "Listen"}</span>
            <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
              {SPEEDS.map((s) => <option key={s} value={s}>{s}×</option>)}
            </select>
          </label>
          {mode === "readalong" && (
            <button
              className="icon-btn ghost"
              aria-pressed={follow}
              onClick={() => (setFollow(!follow), setDetached(false))}
              aria-label="Follow the narration"
              title="Follow the narration (F)"
            >
              <Follow />
            </button>
          )}
        </div>
        <p id="ra-help" className="ra-help hint">
          {mode === "readalong" && "Click a sentence to play from it. "}
          <kbd>Space</kbd> play or pause · <kbd>←</kbd> <kbd>→</kbd> sentence · <kbd>&lt;</kbd> <kbd>&gt;</kbd> speed
          {mode === "readalong" && <> · <kbd>F</kbd> follow</>}
        </p>
      </div>

      <div className="sr-only" aria-live="polite" aria-atomic="true">{announce}</div>
      <audio
        ref={audio}
        preload="metadata"
        onPlay={() => setPlaying(true)}
        onPause={() => (setPlaying(false), setTime(now()), save())}
        onEnded={onEnded}
        onSeeked={() => setTime(now())}
      />
    </div>
  );
}
