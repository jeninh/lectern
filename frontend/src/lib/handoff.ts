// Cross-device hand-off: "Kindle, 12 min ago: continue here?" instead of a silent jump.
//
// The server keeps one canonical position per user and work (last write wins) and reports
// KOReader's own record next to it. A device offers to jump only when another device saved a
// position after this one last saved or accepted one, and it is somewhere else.

import type { HandoffCandidate, WorkProgressResponse } from "./types";

export interface LocalMark {
  /** Server time (received_at) of the last position this device wrote or accepted. */
  seenAt: string | null;
}

/** All positions the server knows, newest first, as hand-off candidates. */
export function candidates(p: WorkProgressResponse): HandoffCandidate[] {
  const out: HandoffCandidate[] = [];
  if (p.source && p.received_at) {
    out.push({
      source: p.source,
      device: p.device || (p.source === "audio" ? "Audio player" : "Web reader"),
      received_at: p.received_at,
      percent: p.percent,
      raw: p.raw,
      text: p.text,
      audio_s: p.audio_s,
      estimated: p.estimated,
    });
  }
  if (p.koreader) out.push(p.koreader);
  return out.sort((a, b) => (b.received_at ?? "").localeCompare(a.received_at ?? ""));
}

/**
 * The position to offer, or null. ``here`` is where this device is now, in the units of
 * ``mode`` (fraction of the book for reading, seconds for listening); offers that would move
 * less than ``minMove`` are not worth interrupting for.
 */
export function pickHandoff(
  p: WorkProgressResponse,
  mark: LocalMark,
  self: { device: string; mode: "read" | "listen"; here: number | null },
  minMove = self.mode === "read" ? 0.005 : 20,
): HandoffCandidate | null {
  for (const c of candidates(p)) {
    if (mark.seenAt && (c.received_at ?? "") <= mark.seenAt) return null; // nothing newer
    if (c.device === self.device && c.source !== "koreader") continue; // our own write
    const target = self.mode === "read" ? (c.text ? c.percent : null) : c.audio_s;
    if (target == null) continue;
    if (self.here != null && Math.abs(target - self.here) < minMove) return null;
    return c;
  }
  return null;
}

export function describeSource(c: HandoffCandidate): string {
  return c.source === "audio" ? "listening" : c.source === "koreader" ? "KOReader" : "reading";
}
