// @vitest-environment jsdom
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { TracksResponse, Work, WorkProgressResponse } from "../types";

const env = vi.hoisted(() => ({ get: vi.fn(), savePosition: vi.fn(), consume: vi.fn(), queue: vi.fn() }));
vi.mock("../api", () => ({ get: env.get, post: vi.fn() }));
vi.mock("../offline/store", () => ({ getBook: vi.fn(async () => null), orOffline: async (online: () => Promise<unknown>) => online() }));
vi.mock("../offline/sync", () => ({ savePosition: env.savePosition }));
vi.mock("../listeningQueue", () => ({ consumeListening: env.consume, listeningQueue: env.queue }));
vi.mock("../store", () => ({ useStore: () => ({ deviceName: "Phone", toast: vi.fn(), me: null, setPreference: vi.fn() }) }));

import { PlayerProvider, usePlayer } from "../player";

const first = { id: 1, title: "Finished", author: "A" } as Work;
const queued = { id: 2, title: "Queued", author: "B" } as Work;
const tracks = (id: number): TracksResponse => ({
  work_id: id, audio_fingerprint: "audio", total_duration_s: 100, alignment: null, transcode: null,
  data_saver: { off: null, unavailable: null, tracks_ready: 0, original_bytes: 0, low_bytes: 0 },
  tracks: [{ id, position: 0, media_type: "audio/mpeg", codec: "mp3", duration_s: 100, offset_s: 0, size: 1, sha256: "x", chapters: [], stream_url: `/audio/${id}`, download_url: `/download/${id}`, variants: { low: null } }],
});
const progress = (id: number): WorkProgressResponse => ({ work_id: id, position: null, source: "audio", raw: null, device: "Laptop", received_at: "2026-10-30T12:00:00Z", text: null, audio_s: 90, percent: 0.9, estimated: false, count: 0, total_duration_s: 100, koreader: null });

let player: ReturnType<typeof usePlayer>;
function Capture() {
  player = usePlayer();
  return null;
}

describe("whole-book queue playback", () => {
  let root: ReturnType<typeof createRoot> | null = null;

  beforeAll(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  });
  afterEach(() => {
    act(() => root?.unmount());
    document.body.replaceChildren();
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

  it("starts a queued finished book at zero instead of its saved completion point", async () => {
    env.get.mockImplementation((url: string) => Promise.resolve(url.endsWith("/tracks") ? tracks(Number(url.split("/")[3])) : progress(Number(url.split("/")[3]))));
    env.savePosition.mockResolvedValue(null);
    env.consume.mockResolvedValue(undefined);
    env.queue.mockResolvedValue({ version: 1, items: [{ id: 8, position: 0, work: queued, track_id: null, track_position: null, chapter_start_s: null, chapter_end_s: null }] });
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);

    const host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    await act(async () => root!.render(createElement(PlayerProvider, null, createElement(Capture))));
    await act(async () => player.open(first));
    await act(async () => {
      document.querySelector("audio")!.dispatchEvent(new Event("ended"));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(env.consume).toHaveBeenCalledWith(8);
    expect(player.work?.id).toBe(queued.id);
    expect(player.t).toBe(0);
  });

  it("keeps a queued chapter's explicit start and end instead of restarting the book", async () => {
    env.get.mockImplementation((url: string) => Promise.resolve(url.endsWith("/tracks") ? tracks(Number(url.split("/")[3])) : progress(Number(url.split("/")[3]))));
    env.savePosition.mockResolvedValue(null);
    env.consume.mockResolvedValue(undefined);
    env.queue.mockResolvedValue({ version: 1, items: [{ id: 8, position: 0, work: queued, track_id: null, track_position: null, chapter_start_s: 30, chapter_end_s: 45 }] });
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);

    const host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    await act(async () => root!.render(createElement(PlayerProvider, null, createElement(Capture))));
    await act(async () => player.open(first));
    await act(async () => {
      document.querySelector("audio")!.dispatchEvent(new Event("ended"));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(player.work?.id).toBe(queued.id);
    expect(player.t).toBe(30);
  });
});
