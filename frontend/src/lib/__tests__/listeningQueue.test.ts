// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

const env = vi.hoisted(() => ({ rows: new Map<string, unknown>(), get: vi.fn(), post: vi.fn() }));
vi.mock("../offline/db", () => ({
  idb: {
    get: async (_store: string, key: string) => env.rows.get(key),
    all: async () => [...env.rows.values()],
    put: async (_store: string, key: string, value: unknown) => void env.rows.set(key, value),
    del: async (_store: string, key: string) => void env.rows.delete(key),
  },
}));
vi.mock("../api", async (orig) => ({ ...(await orig<typeof import("../api")>()), get: env.get, post: env.post }));

import { ApiError, OfflineError } from "../api";
import { clearListening, consumeListening, enqueueListening, flushListeningQueue, listeningQueue } from "../listeningQueue";
import { chapterEndReached } from "../player";

const item = (id: number, workId = id) => ({ id, position: id - 1, work: { id: workId, title: `Book ${workId}`, author: "A" }, track_id: null, track_position: null, chapter_start_s: null, chapter_end_s: null });

describe("offline listening queue", () => {
  beforeEach(() => {
    env.rows.clear();
    env.get.mockReset();
    env.post.mockReset();
    vi.stubGlobal("crypto", { randomUUID: (() => `queue-key-${Math.random()}`) });
  });

  it("keeps an offline enqueue and replays its original idempotency key once", async () => {
    env.post.mockRejectedValueOnce(new OfflineError());
    await enqueueListening(7);
    const pending = [...env.rows.values()].find((x) => (x as { operation?: string }).operation === "enqueue") as { id: string };
    env.post.mockResolvedValue({ version: 1, items: [item(1, 7)] });
    await flushListeningQueue();
    expect(env.post.mock.calls[1][2]).toEqual({ "idempotency-key": pending.id });
    env.get.mockRejectedValueOnce(new OfflineError());
    expect(await listeningQueue()).toEqual({ version: 1, items: [item(1, 7)] });
  });

  it("rebases offline consume and clear without deleting another device's new item", async () => {
    env.rows.set("state", { version: 2, items: [item(1), item(2)] });
    env.post.mockRejectedValue(new OfflineError());
    await consumeListening(1);
    await clearListening();
    // Reconnect: the other device added item 3. Each stale operation gets a fresh base.
    env.post.mockReset();
    env.post.mockRejectedValueOnce(new ApiError(409, "changed", "queue_changed"));
    env.get.mockResolvedValueOnce({ version: 3, items: [item(1), item(2), item(3)] });
    env.post.mockResolvedValueOnce({ version: 4, items: [item(2), item(3)] });
    env.post.mockRejectedValueOnce(new ApiError(409, "changed", "queue_changed"));
    env.get.mockResolvedValueOnce({ version: 4, items: [item(2), item(3)] });
    env.post.mockResolvedValueOnce({ version: 5, items: [item(3)] });
    await flushListeningQueue();
    env.get.mockRejectedValueOnce(new OfflineError());
    expect(await listeningQueue()).toEqual({ version: 5, items: [item(3)] });
  });
});

describe("queued chapter boundary", () => {
  it("advances at the selected chapter end, not the containing track end", () => {
    expect(chapterEndReached(29.99, 30)).toBe(false);
    expect(chapterEndReached(30, 30)).toBe(true);
    expect(chapterEndReached(90, null)).toBe(false);
  });
});
