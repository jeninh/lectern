// @vitest-environment jsdom
// Playback speed per book (issue #71): which speed a book opens at, the one-time move of the
// old per-browser speed, a speed changed offline riding the position outbox, and the sleep
// timer's fade and rewind.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const env = vi.hoisted(() => ({ online: true, csrf: "token", rows: new Map<string, unknown>(), put: vi.fn() }));
vi.mock("../offline/net", () => ({ isOnline: () => env.online }));
vi.mock("../offline/db", () => ({
  idb: {
    all: async () => [...env.rows.values()],
    put: async (_store: string, key: string, v: unknown) => void env.rows.set(key, v),
    del: async (_store: string, key: string) => void env.rows.delete(key),
  },
}));
vi.mock("../api", async (orig) => ({ ...(await orig<typeof import("../api")>()), csrf: () => env.csrf, put: env.put }));

import { ApiError } from "../api";
import { flush, queue, resolveConflict, savePosition } from "../offline/sync";
import { legacyRate, pickRate, sleepRewind, sleepStep } from "../playback";

const at = (min: number) => `2026-10-28T10:${String(min).padStart(2, "0")}:00Z`;

describe("pickRate", () => {
  it("keeps each book's own speed and gives books never played the default", () => {
    // Book A at 1.5x, book B at 1x, both saved from this device and seen back.
    expect(pickRate({ speed: 1.5, received_at: at(5) }, { rate: 1.5, seenAt: at(5) }, 1.25)).toBe(1.5);
    expect(pickRate({ speed: 1, received_at: at(6) }, { rate: 1, seenAt: at(6) }, 1.25)).toBe(1);
    expect(pickRate({ speed: null, received_at: null }, null, 1.25)).toBe(1.25);
    // Played before speeds were kept (no speed stored anywhere): the default too.
    expect(pickRate({ speed: null, received_at: at(1) }, { seenAt: at(1) }, 1.25)).toBe(1.25);
  });

  it("opens at another device's speed on a device that never played the book", () => {
    expect(pickRate({ speed: 1.75, received_at: at(5) }, null, 1)).toBe(1.75);
  });

  it("takes a speed another device saved later, but not an older one over this device's own", () => {
    // Laptop chose 2x, then the phone saved 1.5x at 10:20: the laptop follows.
    expect(pickRate({ speed: 1.5, received_at: at(20) }, { rate: 2, seenAt: at(10) }, 1)).toBe(1.5);
    // This device changed to 2x offline (still queued); the server still holds what it saw.
    expect(pickRate({ speed: 1.5, received_at: at(10) }, { rate: 2, seenAt: at(10) }, 1)).toBe(2);
    // Offline snapshot from before speeds existed: this device's own choice.
    expect(pickRate({ received_at: at(30) }, { rate: 2, seenAt: at(10) }, 1)).toBe(2);
  });
});

describe("legacyRate", () => {
  const store = (v: Record<string, string>) => ({ getItem: (k: string) => v[k] ?? null });
  it("takes the player's old global speed, else read-along's listening speed", () => {
    expect(legacyRate(store({ "lectern.playbackRate": "1.5", "lectern.speed.listen": "2" }))).toBe(1.5);
    expect(legacyRate(store({ "lectern.speed.listen": "2" }))).toBe(2);
    expect(legacyRate(store({}))).toBeNull();
    expect(legacyRate(store({ "lectern.playbackRate": "\"fast\"", "lectern.speed.listen": "{" }))).toBeNull();
  });
});

describe("sleep timer", () => {
  const timed = { kind: "time" as const, endsAt: 100_000, minutes: 5 };
  it("plays at full volume until the last 10 s, fades, then stops", () => {
    expect(sleepStep(timed, 89_000, 0, 1)).toEqual({ stop: false, volume: 1 });
    expect(sleepStep(timed, 95_000, 0, 1)).toEqual({ stop: false, volume: 0.5 });
    expect(sleepStep(timed, 99_900, 0, 1)).toEqual({ stop: false, volume: 0.05 });
    expect(sleepStep(timed, 100_000, 0, 1)).toEqual({ stop: true });
  });

  it("times a timed sleep by the clock, whatever the speed", () => {
    expect(sleepStep(timed, 95_000, 0, 3)).toEqual({ stop: false, volume: 0.5 });
  });

  it("counts end of chapter in listening time at the current speed", () => {
    const chapter = { kind: "chapter" as const, end: 600 };
    // 30 s of audio left at 2x is 15 s of listening: not fading yet.
    expect(sleepStep(chapter, 0, 570, 2)).toEqual({ stop: false, volume: 1 });
    // 10 s of audio at 2x is 5 s: half volume.
    expect(sleepStep(chapter, 0, 590, 2)).toEqual({ stop: false, volume: 0.5 });
    expect(sleepStep(chapter, 0, 600, 2)).toEqual({ stop: true });
  });

  it("rewinds to the start of the last sentence heard, or 5 s when the server cannot say", async () => {
    expect(await sleepRewind(312.4, async () => ({ t: 305.1 }))).toBe(305.1);
    expect(await sleepRewind(312.4, async () => ({ t: 400 }))).toBe(312.4); // never forward
    expect(await sleepRewind(312.4, async () => Promise.reject(new Error("offline")))).toBeCloseTo(307.4);
    expect(await sleepRewind(3, async () => Promise.reject(new Error("offline")))).toBe(0);
  });
});

describe("a speed changed offline", () => {
  const book = { kind: "work" as const, id: 7, workId: 7, title: "Magi" };
  const saved = new Map<string, string>();
  beforeEach(() => {
    env.rows.clear();
    env.put.mockReset();
    saved.clear();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => saved.get(k) ?? null,
      setItem: (k: string, v: string) => void saved.set(k, v),
      removeItem: (k: string) => void saved.delete(k),
    });
    env.online = false;
  });
  afterEach(() => vi.unstubAllGlobals());

  it("is queued with the position and sent with it on reconnect", async () => {
    expect(await savePosition(book, { source: "audio", device: "Phone", audio_s: 120, speed: 1 })).toBeNull();
    expect(await savePosition(book, { source: "audio", device: "Phone", audio_s: 121, speed: 1.5 })).toBeNull();
    expect(env.put).not.toHaveBeenCalled();
    env.online = true;
    env.put.mockResolvedValue({ received_at: at(30), speed: 1.5, audio_s: 121 });
    await flush();
    expect(env.put).toHaveBeenCalledTimes(1);
    const [url, body] = env.put.mock.calls[0];
    expect(url).toBe("/api/works/7/progress");
    expect(body).toMatchObject({ source: "audio", audio_s: 121, speed: 1.5, offline: { base: null } });
    expect(await queue.list()).toEqual([]);
    expect(JSON.parse(localStorage.getItem("lectern.listen.7")!).seenAt).toBe(at(30));
  });

  it("gives way to the other device's speed when its position is chosen", async () => {
    localStorage.setItem("lectern.listen.7", JSON.stringify({ t: 121, seenAt: at(1), rate: 2 }));
    await savePosition(book, { source: "audio", device: "Phone", audio_s: 121, speed: 2 });
    env.online = true;
    const server = { source: "audio", device: "Laptop", received_at: at(40), audio_s: 300, speed: 1.25 };
    env.put.mockRejectedValue(new ApiError(409, "conflict", "progress_conflict", { server, recorded_at: at(20) }));
    await flush();
    const [entry] = await queue.list();
    expect(entry.conflict?.server).toEqual(server);
    await resolveConflict(entry, "theirs");
    expect(JSON.parse(localStorage.getItem("lectern.listen.7")!)).toEqual({ t: 300, seenAt: at(40), rate: 1.25 });
  });
});
