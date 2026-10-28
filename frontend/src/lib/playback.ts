// Playback speed per book and the sleep timer: the pure decisions behind lib/player.tsx and the
// read-along listen mode, so they are unit-tested.
//
// Each book keeps the listening speed last used for it, on the server next to its position
// (`speed` in GET/PUT /api/works/:id/progress), and it travels with the audio position, through
// the offline queue too. Books never played open at the person's default
// (`preferences.playback_speed`), which starts as the one speed this browser used to keep for
// every book.

export type Sleep = { kind: "time"; endsAt: number; minutes: number } | { kind: "chapter"; end: number } | null;

/** Where each browser kept its one speed for every book before speeds were kept per book. */
export const LEGACY_RATE_KEYS = ["lectern.playbackRate", "lectern.speed.listen"];

export function legacyRate(storage: Pick<Storage, "getItem"> = localStorage): number | null {
  for (const key of LEGACY_RATE_KEYS) {
    try {
      const v = JSON.parse(storage.getItem(key) || "null");
      if (typeof v === "number" && v >= 0.5 && v <= 4) return v;
    } catch {
      /* unreadable: try the next */
    }
  }
  return null;
}

/** The speed to open a book at. This device's own latest choice (`mark.rate`, possibly still
 * waiting in the offline queue) wins unless the server has a speed saved after the last
 * position this device saw (`mark.seenAt`), which came from another device; then that one.
 * A book with neither plays at `fallback`, the person's default. */
export function pickRate(
  server: { speed?: number | null; received_at: string | null } | null | undefined,
  mark: { rate?: number | null; seenAt: string | null } | null,
  fallback: number,
): number {
  const theirs = server?.speed ?? null;
  const newer = theirs != null && !!server?.received_at && (!mark?.seenAt || server.received_at > mark.seenAt);
  if (mark?.rate != null && !newer) return mark.rate;
  return theirs ?? mark?.rate ?? fallback;
}

/** Seconds of fade before the sleep timer pauses. */
export const SLEEP_FADE_S = 10;
/** How far back to go when the server cannot say where the last sentence began (offline). */
export const SLEEP_REWIND_S = 5;

/**
 * One tick of the sleep timer: the volume to play at, or `stop`. A timed sleep counts wall-clock
 * time; "end of chapter" counts listening time left at the current speed. The last
 * `SLEEP_FADE_S` seconds fade out (never quite silent, so the fade itself is audible).
 */
export function sleepStep(sleep: NonNullable<Sleep>, nowMs: number, t: number, rate: number): { stop: true } | { stop: false; volume: number } {
  const left = sleep.kind === "time" ? (sleep.endsAt - nowMs) / 1000 : (sleep.end - t) / rate;
  if (left > SLEEP_FADE_S) return { stop: false, volume: 1 };
  if (left > 0) return { stop: false, volume: Math.max(0.05, left / SLEEP_FADE_S) };
  return { stop: true };
}

/** Where to leave the listener after the sleep timer paused at `t`: the start of the sentence
 * heard last, as the server finds it (`ask`: GET /api/works/:id/rewind-point), so the next play
 * repeats what was said while falling asleep; `SLEEP_REWIND_S` back when it cannot be asked. */
export async function sleepRewind(t: number, ask: (t: number) => Promise<{ t: number }>): Promise<number> {
  try {
    return Math.max(0, Math.min(t, (await ask(t)).t));
  } catch {
    return Math.max(0, t - SLEEP_REWIND_S);
  }
}
