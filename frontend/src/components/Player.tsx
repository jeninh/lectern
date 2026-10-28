// Audio player UI: the persistent mini-player in the shell and the pieces the full player on
// the Listening page shares with it (scrubber, speed and sleep menus, hand-off prompt).

import { useState } from "react";
import { Link } from "react-router-dom";
import { relativeTime } from "../lib/format";
import { clock, RATES, usePlayer } from "../lib/player";
import { useStore } from "../lib/store";
import { Cover, tintStyle } from "./Cover";
import { Back, Close, Expand, Forward, NextChapter, Pause, Play, PrevChapter, Timer } from "./Icons";
import { OverflowMenu } from "./Menu";

export function PlayButton({ big }: { big?: boolean }) {
  const p = usePlayer();
  return (
    <button className={`play-btn${big ? " big" : ""}`} onClick={p.toggle} aria-label={p.playing ? "Pause" : "Play"} aria-keyshortcuts="Space" data-loading={p.loading || undefined}>
      {p.playing ? <Pause /> : <Play />}
    </button>
  );
}

export function SkipButtons({ children }: { children?: React.ReactNode }) {
  const p = usePlayer();
  return (
    <>
      <button className="icon-btn ghost" onClick={() => p.skip(-15)} aria-label="Back 15 seconds" title="Back 15 s (←)"><Back /></button>
      {children}
      <button className="icon-btn ghost" onClick={() => p.skip(30)} aria-label="Forward 30 seconds" title="Forward 30 s (→)"><Forward /></button>
    </>
  );
}

export function Scrubber({ compact }: { compact?: boolean }) {
  const p = usePlayer();
  const [drag, setDrag] = useState<number | null>(null);
  const value = drag ?? p.t;
  const title = p.work ? `${p.work.title} position` : "Position";
  if (compact) {
    return (
      <span className="mini-progress" aria-hidden="true">
        <span style={{ width: `${p.duration ? (p.t / p.duration) * 100 : 0}%` }} />
      </span>
    );
  }
  return (
    <div className="scrubber">
      <input
        type="range"
        min={0}
        max={Math.max(1, Math.floor(p.duration))}
        step={1}
        value={Math.floor(value)}
        aria-label={title}
        aria-valuetext={`${clock(value)} of ${clock(p.duration)}`}
        style={{ "--fill": `${p.duration ? (value / p.duration) * 100 : 0}%` } as React.CSSProperties}
        onChange={(e) => setDrag(Number(e.target.value))}
        onPointerUp={() => drag != null && (p.seek(drag), setDrag(null))}
        onKeyUp={() => drag != null && (p.seek(drag), setDrag(null))}
        onBlur={() => drag != null && (p.seek(drag), setDrag(null))}
      />
      <div className="scrub-times">
        <span>{clock(value)}</span>
        <span>{p.chapter?.title}</span>
        <span>−{clock((p.duration - value) / p.rate)}</span>
      </div>
    </div>
  );
}

export function RateMenu() {
  const p = usePlayer();
  return (
    <OverflowMenu
      label={`Playback speed, ${p.rate}×`}
      className="btn sm ghost"
      up
      trigger={<span className="tabular">{p.rate}×</span>}
      actions={[
        ...RATES.map((r) => ({ label: `${r}×`, checked: p.rate === r, onSelect: () => p.setRate(r) })),
        // Each book keeps its own speed; this one is for books not played yet.
        ...(p.rate !== p.defaultRate ? [{ label: `Use ${p.rate}× for new books`, onSelect: p.setDefaultRate }] : []),
      ]}
    />
  );
}

export function sleepLabel(s: ReturnType<typeof usePlayer>["sleep"], now: number): string {
  if (!s) return "Sleep";
  if (s.kind === "chapter") return "End of chapter";
  return `${Math.max(1, Math.ceil((s.endsAt - now) / 60000))} min`;
}

export function SleepMenu() {
  const p = usePlayer();
  const { now } = useStore();
  const active = p.sleep;
  return (
    <OverflowMenu
      label={active ? `Sleep timer: ${sleepLabel(active, now)}` : "Sleep timer"}
      className={`btn sm ghost${active ? " on" : ""}`}
      up
      trigger={<><Timer /> <span>{sleepLabel(active, now)}</span></>}
      actions={[
        ...[5, 15, 30, 45, 60].map((m) => ({ label: `${m} minutes`, checked: active?.kind === "time" && active.minutes === m, onSelect: () => p.setSleep(m) })),
        { label: "End of chapter", checked: active?.kind === "chapter", onSelect: () => p.setSleep("chapter") },
        ...(active ? [{ label: "Turn off", onSelect: () => p.setSleep(null) }] : []),
      ]}
    />
  );
}

/** "Kindle, 12 min ago: continue here?" */
export function HandoffPrompt({ compact }: { compact?: boolean }) {
  const p = usePlayer();
  const { now } = useStore();
  const h = p.handoff;
  if (!h || h.audio_s == null) return null;
  return (
    <div className={`handoff${compact ? " compact" : ""}`} role="status">
      <span>
        <strong>{h.device}</strong>, {relativeTime(h.received_at, now)}: at {clock(h.audio_s)}
        {h.estimated && <span className="hint"> (estimated)</span>}. Continue there?
      </span>
      <span className="row">
        <button className="btn sm primary" onClick={p.acceptHandoff}>Jump to {clock(h.audio_s)}</button>
        <button className="btn sm ghost" onClick={p.dismissHandoff}>Stay here</button>
      </span>
    </div>
  );
}

/** Bottom bar that keeps playing while you browse; above the tab bar on phones. */
export function MiniPlayer() {
  const p = usePlayer();
  if (!p.work) return null;
  const w = p.work;
  return (
    <section className="mini-player" style={tintStyle(w)} aria-label="Now playing">
      <HandoffPrompt compact />
      <div className="mini-row">
        <Link to="/app/listening" className="mini-meta" aria-label={`Open player: ${w.title}`}>
          <Cover work={w} />
          <span className="titles">
            <strong>{w.title}</strong>
            <span>{p.chapter?.title ?? w.author} · {clock(p.t)}</span>
          </span>
        </Link>
        <div className="mini-controls">
          <span className="desktop-only">
            {p.chapters.length > 1 && <button className="icon-btn ghost" onClick={p.prevChapter} aria-label="Previous chapter" title="Previous chapter"><PrevChapter /></button>}
            <SkipButtons><PlayButton /></SkipButtons>
            {p.chapters.length > 1 && <button className="icon-btn ghost" onClick={p.nextChapter} aria-label="Next chapter" title="Next chapter"><NextChapter /></button>}
          </span>
          <span className="mobile-inline"><PlayButton /></span>
          <Link className="icon-btn ghost desktop-only" to="/app/listening" aria-label="Open player" title="Open player"><Expand /></Link>
          <button className="icon-btn ghost sm" onClick={p.close} aria-label="Stop and close player" title="Close"><Close /></button>
        </div>
      </div>
      <Scrubber compact />
    </section>
  );
}
