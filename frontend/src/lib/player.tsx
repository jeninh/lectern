// The audiobook player: one <audio> element for the whole app, so playback continues while you
// browse, with the mini-player in the shell and the full player on the Listening page.
//
// A work's tracks sit end to end on one timeline (offset_s); everything here speaks work-time
// seconds and maps to (track, seconds into the file) only when touching the element.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { get, post, put } from "./api";
import { pickHandoff } from "./handoff";
import { useLocalStorage } from "./hooks";
import { useStore } from "./store";
import type { HandoffCandidate, Track, TracksResponse, Work, WorkProgressResponse } from "./types";

export interface GlobalChapter {
  title: string;
  start: number;
  end: number;
}

export type Sleep = { kind: "time"; endsAt: number; minutes: number } | { kind: "chapter"; end: number } | null;

interface Player {
  work: Work | null;
  tracks: Track[];
  duration: number;
  chapters: GlobalChapter[];
  chapter: GlobalChapter | null;
  t: number;
  playing: boolean;
  loading: boolean;
  rate: number;
  sleep: Sleep;
  dataSaver: boolean;
  usingLow: boolean;
  handoff: HandoffCandidate | null;
  open: (work: Work, opts?: { autoplay?: boolean; at?: number }) => Promise<void>;
  close: () => void;
  toggle: () => void;
  play: () => void;
  pause: () => void;
  seek: (t: number) => void;
  skip: (delta: number) => void;
  setRate: (r: number) => void;
  setSleep: (s: "chapter" | number | null) => void;
  setDataSaver: (v: boolean) => Promise<void>;
  nextChapter: () => void;
  prevChapter: () => void;
  acceptHandoff: () => void;
  dismissHandoff: () => void;
}

const Ctx = createContext<Player | null>(null);
export const RATES = [0.8, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3];
const SAVE_EVERY_MS = 15000;

interface Mark {
  t: number;
  seenAt: string | null;
}
const markKey = (id: number) => `lectern.listen.${id}`;
function readMark(id: number): Mark | null {
  try {
    return JSON.parse(localStorage.getItem(markKey(id)) || "null");
  } catch {
    return null;
  }
}
function writeMark(id: number, m: Mark) {
  try {
    localStorage.setItem(markKey(id), JSON.stringify(m));
  } catch {
    /* quota */
  }
}

export function chaptersOf(tracks: Track[]): GlobalChapter[] {
  const flat = tracks.flatMap((tr) =>
    (tr.chapters.length ? tr.chapters : [{ title: `Part ${tr.position + 1}`, start_s: 0 }]).map((c) => ({ title: c.title, start: tr.offset_s + c.start_s })),
  );
  const total = tracks.reduce((s, tr) => s + tr.duration_s, 0);
  return flat.map((c, i) => ({ ...c, end: flat[i + 1]?.start ?? total }));
}

export function trackAt(tracks: Track[], t: number): number {
  let i = 0;
  while (i + 1 < tracks.length && tracks[i + 1].offset_s <= t) i++;
  return i;
}

export function PlayerProvider({ children }: { children: ReactNode }) {
  const { deviceName, toast } = useStore();
  const audio = useRef<HTMLAudioElement | null>(null);
  const [work, setWork] = useState<Work | null>(null);
  const [tracks, setTracks] = useState<Track[]>([]);
  const [index, setIndex] = useState(0);
  const [t, setT] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [loading, setLoading] = useState(false);
  const [rate, setRateState] = useLocalStorage<number>("lectern.playbackRate", 1);
  const [dataSaver, setDataSaverState] = useLocalStorage<boolean>("lectern.dataSaver", false);
  const [sleep, setSleepState] = useState<Sleep>(null);
  const [handoff, setHandoff] = useState<HandoffCandidate | null>(null);
  const [usingLow, setUsingLow] = useState(false);
  const state = useRef({ work: null as Work | null, tracks: [] as Track[], index: 0, pendingSeek: null as number | null, lastSave: 0, listened: new Set<number>() });

  const duration = useMemo(() => tracks.reduce((s, tr) => s + tr.duration_s, 0), [tracks]);
  const chapters = useMemo(() => chaptersOf(tracks), [tracks]);
  const chapter = useMemo(() => [...chapters].reverse().find((c) => c.start <= t + 0.25) ?? chapters[0] ?? null, [chapters, t]);

  const srcFor = useCallback(
    (tr: Track) => (dataSaver && tr.variants.low ? tr.variants.low.stream_url : tr.stream_url),
    [dataSaver],
  );

  const save = useCallback(
    async (at?: number, keepalive = false) => {
      const w = state.current.work;
      const el = audio.current;
      if (!w || !el) return;
      const tr = state.current.tracks[state.current.index];
      const gt = at ?? (tr ? tr.offset_s + el.currentTime : 0);
      state.current.lastSave = Date.now();
      try {
        const r = await put<WorkProgressResponse>(`/api/works/${w.id}/progress`, { source: "audio", device: deviceName, audio_s: Math.round(gt * 100) / 100 }, keepalive);
        writeMark(w.id, { t: gt, seenAt: r.received_at });
      } catch {
        writeMark(w.id, { t: gt, seenAt: readMark(w.id)?.seenAt ?? null });
      }
    },
    [deviceName],
  );

  const load = useCallback(
    (i: number, local: number, autoplay: boolean) => {
      const el = audio.current;
      const tr = state.current.tracks[i];
      if (!el || !tr) return;
      state.current.index = i;
      setIndex(i);
      const src = srcFor(tr);
      setUsingLow(src !== tr.stream_url);
      if (el.getAttribute("src") !== src) {
        state.current.pendingSeek = local;
        el.src = src;
        el.load();
      } else {
        el.currentTime = local;
      }
      el.playbackRate = rate;
      if (autoplay) el.play().catch(() => {});
    },
    [srcFor, rate],
  );

  const seek = useCallback(
    (gt: number) => {
      const trs = state.current.tracks;
      if (!trs.length) return;
      const total = trs.reduce((s, tr) => s + tr.duration_s, 0);
      gt = Math.max(0, Math.min(gt, total - 0.05));
      const i = trackAt(trs, gt);
      const el = audio.current;
      load(i, gt - trs[i].offset_s, !!el && !el.paused);
      setT(gt);
    },
    [load],
  );

  const open = useCallback(
    async (w: Work, opts: { autoplay?: boolean; at?: number } = {}) => {
      if (state.current.work && state.current.work.id !== w.id) await save();
      setLoading(true);
      try {
        const [tr, prog] = await Promise.all([
          get<TracksResponse>(`/api/works/${w.id}/tracks`),
          get<WorkProgressResponse>(`/api/works/${w.id}/progress`),
        ]);
        if (!tr.tracks.length) throw new Error("This book has no audio yet.");
        state.current.work = w;
        state.current.tracks = tr.tracks;
        setWork(w);
        setTracks(tr.tracks);
        const mark = readMark(w.id);
        let start = opts.at ?? mark?.t ?? null;
        if (start == null && prog.audio_s != null) {
          start = prog.audio_s; // a fresh device picks up the latest known place
          writeMark(w.id, { t: start, seenAt: prog.received_at });
        }
        start = start ?? 0;
        setHandoff(opts.at == null && mark ? pickHandoff(prog, { seenAt: mark.seenAt }, { device: deviceName, mode: "listen", here: start }) : null);
        const i = trackAt(tr.tracks, start);
        setT(start);
        load(i, start - tr.tracks[i].offset_s, !!opts.autoplay);
      } catch (e) {
        toast((e as Error).message, "error");
      } finally {
        setLoading(false);
      }
    },
    [deviceName, load, save, toast],
  );

  const play = useCallback(() => {
    audio.current?.play().catch((e) => toast(`Could not play: ${(e as Error).message}`, "error"));
  }, [toast]);
  const pause = useCallback(() => audio.current?.pause(), []);
  const toggle = useCallback(() => (audio.current?.paused ? play() : pause()), [play, pause]);
  const skip = useCallback((d: number) => {
    const el = audio.current;
    const tr = state.current.tracks[state.current.index];
    if (!el || !tr) return;
    seek(tr.offset_s + el.currentTime + d);
  }, [seek]);
  const nextChapter = useCallback(() => {
    const c = chapters.find((x) => x.start > t + 0.5);
    if (c) seek(c.start);
  }, [chapters, t, seek]);
  const prevChapter = useCallback(() => {
    // Within the first 3 s of a chapter, go to the previous one; otherwise restart this one.
    const cur = [...chapters].reverse().find((x) => x.start <= t + 0.25);
    if (!cur) return seek(0);
    if (t - cur.start > 3) return seek(cur.start);
    const i = chapters.indexOf(cur);
    seek(chapters[Math.max(0, i - 1)].start);
  }, [chapters, t, seek]);

  const setRate = useCallback((r: number) => {
    setRateState(r);
    if (audio.current) audio.current.playbackRate = r;
  }, [setRateState]);

  const setSleep = useCallback((s: "chapter" | number | null) => {
    if (s === null) setSleepState(null);
    else if (s === "chapter") setSleepState(chapter ? { kind: "chapter", end: chapter.end } : null);
    else setSleepState({ kind: "time", endsAt: Date.now() + s * 60000, minutes: s });
    if (audio.current) audio.current.volume = 1;
  }, [chapter]);

  const setDataSaver = useCallback(async (v: boolean) => {
    const w = state.current.work;
    if (v && w) {
      // A data-saver copy may have been made since the tracks were loaded.
      try {
        const tr = await get<TracksResponse>(`/api/works/${w.id}/tracks`);
        if (state.current.work?.id === w.id) {
          state.current.tracks = tr.tracks;
          setTracks(tr.tracks);
        }
      } catch {
        /* keep what we have */
      }
    }
    setDataSaverState(v);
  }, [setDataSaverState]);
  // Switching renditions keeps the place (timings are the same in both).
  useEffect(() => {
    const el = audio.current;
    const tr = state.current.tracks[state.current.index];
    if (!el || !tr) return;
    const want = srcFor(tr);
    if (el.getAttribute("src") && el.getAttribute("src") !== want) load(state.current.index, el.currentTime, !el.paused);
  }, [srcFor, load, tracks]);

  const close = useCallback(() => {
    save().catch(() => {});
    audio.current?.pause();
    audio.current?.removeAttribute("src");
    state.current.work = null;
    state.current.tracks = [];
    setWork(null);
    setTracks([]);
    setSleepState(null);
    setHandoff(null);
  }, [save]);

  // Sleep timer: fade over the last 10 s, then pause and rewind to the start of the sentence
  // being heard (exact once aligned; otherwise the last pause in the audio).
  useEffect(() => {
    if (!sleep || !work) return;
    const id = window.setInterval(async () => {
      const el = audio.current;
      if (!el) return;
      const tr = state.current.tracks[state.current.index];
      const gt = tr ? tr.offset_s + el.currentTime : 0;
      const left = sleep.kind === "time" ? (sleep.endsAt - Date.now()) / 1000 : (sleep.end - gt) / rate;
      if (left > 10) {
        el.volume = 1;
        return;
      }
      if (left > 0) {
        el.volume = Math.max(0.05, left / 10);
        return;
      }
      window.clearInterval(id);
      el.pause();
      el.volume = 1;
      setSleepState(null);
      let back = gt;
      try {
        back = (await get<{ t: number; method: string }>(`/api/works/${work.id}/rewind-point?t=${gt.toFixed(2)}`)).t;
      } catch {
        back = Math.max(0, gt - 5);
      }
      seek(back);
      await save(back);
      toast("Sleep timer: paused at the start of the last sentence you heard.");
    }, 500);
    return () => window.clearInterval(id);
  }, [sleep, work, rate, seek, save, toast]);

  // Element events.
  useEffect(() => {
    const el = audio.current!;
    const onTime = () => {
      const tr = state.current.tracks[state.current.index];
      if (tr) setT(tr.offset_s + el.currentTime);
      if (!el.paused && Date.now() - state.current.lastSave > SAVE_EVERY_MS) save().catch(() => {});
    };
    const onMeta = () => {
      if (state.current.pendingSeek != null) {
        el.currentTime = state.current.pendingSeek;
        state.current.pendingSeek = null;
      }
      el.playbackRate = rate;
    };
    const onPlay = () => {
      setPlaying(true);
      const w = state.current.work;
      if (w && !state.current.listened.has(w.id)) {
        state.current.listened.add(w.id);
        post(`/api/works/${w.id}/listen`).catch(() => {});
      }
    };
    const onPause = () => {
      setPlaying(false);
      save().catch(() => {});
    };
    const onEnded = () => {
      const i = state.current.index + 1;
      if (i < state.current.tracks.length) load(i, 0, true);
      else setPlaying(false);
    };
    const onError = () => {
      const tr = state.current.tracks[state.current.index];
      if (tr && el.getAttribute("src") === tr.variants.low?.stream_url) {
        // The data-saver copy went away; fall back to the original.
        state.current.pendingSeek = el.currentTime;
        el.src = tr.stream_url;
        setUsingLow(false);
        el.play().catch(() => {});
      }
    };
    const onWait = () => setLoading(true);
    const onCan = () => setLoading(false);
    el.addEventListener("timeupdate", onTime);
    el.addEventListener("loadedmetadata", onMeta);
    el.addEventListener("play", onPlay);
    el.addEventListener("pause", onPause);
    el.addEventListener("ended", onEnded);
    el.addEventListener("error", onError);
    el.addEventListener("waiting", onWait);
    el.addEventListener("canplay", onCan);
    const onHide = () => document.hidden && state.current.work && save(undefined, true).catch(() => {});
    document.addEventListener("visibilitychange", onHide);
    return () => {
      el.removeEventListener("timeupdate", onTime);
      el.removeEventListener("loadedmetadata", onMeta);
      el.removeEventListener("play", onPlay);
      el.removeEventListener("pause", onPause);
      el.removeEventListener("ended", onEnded);
      el.removeEventListener("error", onError);
      el.removeEventListener("waiting", onWait);
      el.removeEventListener("canplay", onCan);
      document.removeEventListener("visibilitychange", onHide);
    };
  }, [save, load, rate]);

  // Media Session: lock screen, headphones, car controls.
  useEffect(() => {
    const ms = navigator.mediaSession;
    if (!ms || !work) return;
    ms.metadata = new MediaMetadata({
      title: chapter?.title && chapter.title !== work.title ? `${work.title} · ${chapter.title}` : work.title,
      artist: work.author,
      album: work.title,
      artwork: work.cover_url ? [{ src: new URL(work.cover_url, location.origin).href, sizes: "512x512", type: "image/jpeg" }] : [],
    });
    const handlers: [MediaSessionAction, MediaSessionActionHandler][] = [
      ["play", () => play()],
      ["pause", () => pause()],
      ["seekbackward", (d) => skip(-(d.seekOffset ?? 15))],
      ["seekforward", (d) => skip(d.seekOffset ?? 30)],
      ["previoustrack", () => prevChapter()],
      ["nexttrack", () => nextChapter()],
      ["seekto", (d) => d.seekTime != null && seek(d.seekTime)],
      ["stop", () => pause()],
    ];
    for (const [a, h] of handlers) {
      try {
        ms.setActionHandler(a, h);
      } catch {
        /* unsupported action */
      }
    }
  }, [work, chapter, play, pause, skip, seek, nextChapter, prevChapter]);
  useEffect(() => {
    const ms = navigator.mediaSession;
    if (!ms || !work || !duration) return;
    ms.playbackState = playing ? "playing" : "paused";
    try {
      ms.setPositionState({ duration, position: Math.min(t, duration), playbackRate: rate });
    } catch {
      /* invalid state while loading */
    }
  }, [work, duration, t, playing, rate]);

  const acceptHandoff = useCallback(() => {
    if (!handoff || handoff.audio_s == null || !work) return;
    seek(handoff.audio_s);
    writeMark(work.id, { t: handoff.audio_s, seenAt: handoff.received_at });
    setHandoff(null);
  }, [handoff, work, seek]);
  const dismissHandoff = useCallback(() => {
    if (handoff && work) writeMark(work.id, { t, seenAt: handoff.received_at });
    setHandoff(null);
  }, [handoff, work, t]);

  const value = useMemo<Player>(
    () => ({ work, tracks, duration, chapters, chapter, t, playing, loading, rate, sleep, dataSaver, usingLow, handoff, open, close, toggle, play, pause, seek, skip, setRate, setSleep, setDataSaver, nextChapter, prevChapter, acceptHandoff, dismissHandoff }),
    [work, tracks, duration, chapters, chapter, t, playing, loading, rate, sleep, dataSaver, usingLow, handoff, open, close, toggle, play, pause, seek, skip, setRate, setSleep, setDataSaver, nextChapter, prevChapter, acceptHandoff, dismissHandoff],
  );
  void index;
  return (
    <Ctx.Provider value={value}>
      {children}
      <audio ref={audio} preload="metadata" hidden />
    </Ctx.Provider>
  );
}

export function usePlayer(): Player {
  const p = useContext(Ctx);
  if (!p) throw new Error("PlayerProvider missing");
  return p;
}

export function clock(s: number): string {
  if (!Number.isFinite(s) || s < 0) s = 0;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}
