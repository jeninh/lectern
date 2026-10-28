// The audiobook player: one <audio> element for the whole app, so playback continues while you
// browse, with the mini-player in the shell and the full player on the Listening page.
//
// A work's tracks sit end to end on one timeline (offset_s); everything here speaks work-time
// seconds and maps to (track, seconds into the file) only when touching the element.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { get, post } from "./api";
import { getBook, orOffline } from "./offline/store";
import { savePosition } from "./offline/sync";
import { pickHandoff } from "./handoff";
import { useLocalStorage } from "./hooks";
import { legacyRate, pickRate, sleepRewind, sleepStep, type Sleep } from "./playback";
import { useStore } from "./store";
import type { HandoffCandidate, Track, TracksResponse, Work, WorkProgressResponse } from "./types";

export interface GlobalChapter {
  title: string;
  start: number;
  end: number;
}

export type { Sleep };

interface Player {
  work: Work | null;
  tracks: Track[];
  duration: number;
  chapters: GlobalChapter[];
  chapter: GlobalChapter | null;
  t: number;
  playing: boolean;
  loading: boolean;
  /** This book's speed (kept per book on the server; lib/playback.ts). */
  rate: number;
  /** The person's speed for books not played yet. */
  defaultRate: number;
  sleep: Sleep;
  /** Whether data saver is in effect here: this device's choice, else the account's default. */
  dataSaver: boolean;
  /** This device's own choice, or null when it follows the account (Security page). */
  dataSaverDevice: boolean | null;
  usingLow: boolean;
  handoff: HandoffCandidate | null;
  open: (work: Work, opts?: { autoplay?: boolean; at?: number }) => Promise<void>;
  close: () => void;
  toggle: () => void;
  play: () => void;
  pause: () => void;
  seek: (t: number) => void;
  /** Play an audiobook clip (issue #81) from its start and pause at its end; opens the book
   * first when another one is loaded. */
  playClip: (work: Work, start: number, end: number | null) => Promise<void>;
  /** The clip being played, until it ends or the listener goes elsewhere. */
  clip: { workId: number; start: number; end: number } | null;
  skip: (delta: number) => void;
  setRate: (r: number) => void;
  /** Make the current speed the default for books not played yet. */
  setDefaultRate: () => void;
  setSleep: (s: "chapter" | number | null) => void;
  /** Choose for this device; null follows the account's default again. */
  setDataSaver: (v: boolean | null) => Promise<void>;
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
  /** The speed this device last used for the book. */
  rate?: number;
}
const markKey = (id: number) => `lectern.listen.${id}`;
/** This device's listening mark for a book: place, last server time seen, and speed. Shared
 * with the read-along listen mode. */
export function readMark(id: number): Mark | null {
  try {
    return JSON.parse(localStorage.getItem(markKey(id)) || "null");
  } catch {
    return null;
  }
}
export function writeMark(id: number, m: Partial<Mark>) {
  try {
    localStorage.setItem(markKey(id), JSON.stringify({ ...readMark(id), ...m }));
  } catch {
    /* quota */
  }
}

/** This device's data-saver choice (null: follow the account). Read when needed, so a sheet
 * opened later sees a change the player made. */
export const DATA_SAVER_KEY = "lectern.dataSaver";
export function deviceDataSaver(): boolean | null {
  try {
    const v = JSON.parse(localStorage.getItem(DATA_SAVER_KEY) || "null");
    return typeof v === "boolean" ? v : null;
  } catch {
    return null;
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
  const { deviceName, toast, me, online, setPreference } = useStore();
  const audio = useRef<HTMLAudioElement | null>(null);
  const [work, setWork] = useState<Work | null>(null);
  const [tracks, setTracks] = useState<Track[]>([]);
  const [index, setIndex] = useState(0);
  const [t, setT] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [loading, setLoading] = useState(false);
  const savedDefault = me?.preferences.playback_speed ?? null;
  const defaultRate = savedDefault ?? legacyRate() ?? 1;
  const [rate, setRateState] = useState(defaultRate);
  const [dataSaverDevice, setDataSaverState] = useLocalStorage<boolean | null>(DATA_SAVER_KEY, null);
  const dataSaver = dataSaverDevice ?? !!me?.preferences.data_saver;
  const [sleep, setSleepState] = useState<Sleep>(null);
  const [handoff, setHandoff] = useState<HandoffCandidate | null>(null);
  const [usingLow, setUsingLow] = useState(false);
  const [clip, setClip] = useState<{ workId: number; start: number; end: number } | null>(null);
  const clipRef = useRef(clip);
  clipRef.current = clip;
  const state = useRef({ work: null as Work | null, tracks: [] as Track[], index: 0, pendingSeek: null as number | null, lastSave: 0, listened: new Set<number>(), local: false, rate: defaultRate });

  const duration = useMemo(() => tracks.reduce((s, tr) => s + tr.duration_s, 0), [tracks]);
  const chapters = useMemo(() => chaptersOf(tracks), [tracks]);
  const chapter = useMemo(() => [...chapters].reverse().find((c) => c.start <= t + 0.25) ?? chapters[0] ?? null, [chapters, t]);

  // A downloaded audiobook always plays the copy on this device (its stream_url points there,
  // and the service worker answers it), whatever the data-saver setting.
  const srcFor = useCallback(
    (tr: Track) => (dataSaver && tr.variants.low && !state.current.local ? tr.variants.low.stream_url : tr.stream_url),
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
        const r = await savePosition<WorkProgressResponse>({ kind: "work", id: w.id, workId: w.id, title: w.title }, { source: "audio", device: deviceName, audio_s: Math.round(gt * 100) / 100, speed: state.current.rate }, keepalive);
        writeMark(w.id, { t: gt, seenAt: r?.received_at ?? readMark(w.id)?.seenAt ?? null });
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
      el.playbackRate = state.current.rate;
      if (autoplay) el.play().catch(() => {});
    },
    [srcFor],
  );

  const seek = useCallback(
    (gt: number) => {
      const trs = state.current.tracks;
      if (!trs.length) return;
      const total = trs.reduce((s, tr) => s + tr.duration_s, 0);
      gt = Math.max(0, Math.min(gt, total - 0.05));
      const i = trackAt(trs, gt);
      const el = audio.current;
      const c = clipRef.current;
      if (c && (gt < c.start - 0.5 || gt > c.end)) setClip(null);
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
        const offline = await getBook(w.id);
        const local = offline?.tracks ?? null;
        const [tr, prog] = await Promise.all([
          local ?? get<TracksResponse>(`/api/works/${w.id}/tracks`),
          orOffline(() => get<WorkProgressResponse>(`/api/works/${w.id}/progress`), async () => offline?.progress),
        ]);
        if (!tr.tracks.length) throw new Error("This book has no audio yet.");
        state.current.local = !!local;
        state.current.work = w;
        state.current.tracks = tr.tracks;
        setWork(w);
        setTracks(tr.tracks);
        const mark = readMark(w.id);
        const r = pickRate(prog, mark, defaultRate);
        state.current.rate = r;
        setRateState(r);
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
    [deviceName, load, save, toast, defaultRate],
  );

  const playClip = useCallback(
    async (w: Work, start: number, end: number | null) => {
      if (state.current.work?.id !== w.id) await open(w, { at: start });
      if (state.current.work?.id !== w.id) return; // could not open it (toasted)
      setClip(end != null ? { workId: w.id, start, end } : null);
      clipRef.current = end != null ? { workId: w.id, start, end } : null;
      seek(start);
      audio.current?.play().catch((e) => toast(`Could not play: ${(e as Error).message}`, "error"));
    },
    [open, seek, toast],
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

  // Each book keeps its own speed: remembered here at once and saved with the position (the
  // offline queue holds it until the server can be reached).
  const setRate = useCallback((r: number) => {
    state.current.rate = r;
    setRateState(r);
    if (audio.current) audio.current.playbackRate = r;
    const w = state.current.work;
    if (w) {
      writeMark(w.id, { rate: r });
      save().catch(() => {});
    }
  }, [save]);
  const setDefaultRate = useCallback(() => {
    setPreference("playback_speed", state.current.rate).catch(() => {});
  }, [setPreference]);
  // Before speeds were kept per book, each browser kept one for every book: the first time the
  // account has no default, that one becomes it.
  const migrating = useRef(false);
  useEffect(() => {
    if (!me || !online || savedDefault != null || migrating.current) return;
    const legacy = legacyRate();
    if (legacy == null) return;
    migrating.current = true;
    setPreference("playback_speed", legacy).catch(() => {});
  }, [me, online, savedDefault, setPreference]);
  useEffect(() => {
    if (!state.current.work) {
      state.current.rate = defaultRate;
      setRateState(defaultRate);
    }
  }, [defaultRate]);

  const setSleep = useCallback((s: "chapter" | number | null) => {
    if (s === null) setSleepState(null);
    else if (s === "chapter") setSleepState(chapter ? { kind: "chapter", end: chapter.end } : null);
    else setSleepState({ kind: "time", endsAt: Date.now() + s * 60000, minutes: s });
    if (audio.current) audio.current.volume = 1;
  }, [chapter]);

  const setDataSaver = useCallback(async (v: boolean | null) => {
    const w = state.current.work;
    if ((v ?? !!me?.preferences.data_saver) && w && !state.current.local) {
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
  }, [setDataSaverState, me?.preferences.data_saver]);
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
    setClip(null);
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
      const step = sleepStep(sleep, Date.now(), gt, rate);
      if (!step.stop) {
        el.volume = step.volume;
        return;
      }
      window.clearInterval(id);
      el.pause();
      el.volume = 1;
      setSleepState(null);
      const back = await sleepRewind(gt, (at) => get<{ t: number; method: string }>(`/api/works/${work.id}/rewind-point?t=${at.toFixed(2)}`));
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
      const c = clipRef.current;
      if (tr && c && c.workId === state.current.work?.id && tr.offset_s + el.currentTime >= c.end) {
        el.pause();
        setClip(null);
      }
      if (!el.paused && Date.now() - state.current.lastSave > SAVE_EVERY_MS) save().catch(() => {});
    };
    const onMeta = () => {
      if (state.current.pendingSeek != null) {
        el.currentTime = state.current.pendingSeek;
        state.current.pendingSeek = null;
      }
      el.playbackRate = state.current.rate;
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
  }, [save, load]);

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

  useEffect(() => {
    const on = (e: Event) => {
      const d = (e as CustomEvent<{ kind: string; id: number; server: { audio_s?: number | null; speed?: number | null } }>).detail;
      if (d.kind !== "work" || d.id !== state.current.work?.id) return;
      if (d.server.audio_s != null) seek(d.server.audio_s);
      if (d.server.speed != null) {
        // "Use the other device's" takes its speed with its position.
        state.current.rate = d.server.speed;
        setRateState(d.server.speed);
        writeMark(d.id, { rate: d.server.speed });
        if (audio.current) audio.current.playbackRate = d.server.speed;
      }
    };
    window.addEventListener("lectern:position", on);
    return () => window.removeEventListener("lectern:position", on);
  }, [seek]);

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
    () => ({ work, tracks, duration, chapters, chapter, t, playing, loading, rate, defaultRate, sleep, dataSaver, dataSaverDevice, usingLow, handoff, open, close, toggle, play, pause, seek, playClip, clip, skip, setRate, setDefaultRate, setSleep, setDataSaver, nextChapter, prevChapter, acceptHandoff, dismissHandoff }),
    [work, tracks, duration, chapters, chapter, t, playing, loading, rate, defaultRate, sleep, dataSaver, dataSaverDevice, usingLow, handoff, open, close, toggle, play, pause, seek, playClip, clip, skip, setRate, setDefaultRate, setSleep, setDataSaver, nextChapter, prevChapter, acceptHandoff, dismissHandoff],
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
