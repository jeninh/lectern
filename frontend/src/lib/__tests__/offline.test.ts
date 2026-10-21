import { describe, expect, it } from "vitest";
import { platformOf } from "../offline/install";
import { PositionQueue, type QueuedPosition, type SendResult } from "../offline/queue";
import { bookKey, parseRange, route } from "../offline/routes";

const O = "https://lectern.example";
const req = (path: string, init: { method?: string; mode?: string; headers?: Record<string, string> } = {}) => ({
  url: `${O}${path}`,
  method: init.method ?? "GET",
  mode: init.mode ?? "cors",
  headers: { get: (n: string) => init.headers?.[n] ?? null },
});

describe("service worker routes", () => {
  it("never answers API, sign-in, OPDS, or other signed-in data from a cache", () => {
    for (const p of ["/api/me", "/api/works/1/progress", "/api/works/1/tracks", "/api/works/1/text/2", "/login", "/logout", "/opds", "/mcp", "/library/files/3/download", "/library/tracks/4/download", "/library/works/1/read-aloud.epub", "/syncs/progress/x"]) {
      expect(route(req(p), O).kind, p).toBe("network");
    }
    // Writes and other origins are never touched either.
    expect(route(req("/library/works/1/book.epub", { method: "POST" }), O).kind).toBe("network");
    expect(route({ ...req("/x"), url: "https://elsewhere.example/library/works/1/book.epub" }, O).kind).toBe("network");
  });

  it("answers downloaded book files by a key that ignores everything but the content selector", () => {
    expect(route(req("/library/works/12/book.epub"), O)).toEqual({ kind: "book", key: "/library/works/12/book.epub" });
    expect(route(req("/library/files/7/view"), O)).toEqual({ kind: "book", key: "/library/files/7/view" });
    expect(route(req("/library/tracks/3/stream?variant=low"), O)).toEqual({ kind: "book", key: "/library/tracks/3/stream?variant=low" });
    // Full quality and data saver are different files.
    expect(route(req("/library/tracks/3/stream"), O)).toEqual({ kind: "book", key: "/library/tracks/3/stream" });
    expect(route(req("/library/works/12/cover.jpg?v=4&junk=1"), O)).toEqual({ kind: "book", key: "/library/works/12/cover.jpg?v=4" });
    expect(bookKey(new URL(`${O}/library/tracks/3/stream?x=1&variant=low`))).toBe("/library/tracks/3/stream?variant=low");
  });

  it("lets the downloader fetch fresh bytes instead of the stored copy", () => {
    expect(route(req("/library/tracks/3/stream", { headers: { "x-lectern-download": "1" } }), O).kind).toBe("network");
    expect(route(req("/library/works/12/book.epub?offline=1"), O).kind).toBe("network");
  });

  it("serves the shell for app pages and caches only public build output", () => {
    expect(route(req("/app", { mode: "navigate" }), O).kind).toBe("navigate");
    expect(route(req("/app/reader/3", { mode: "navigate" }), O).kind).toBe("navigate");
    expect(route(req("/login", { mode: "navigate" }), O).kind).toBe("network");
    expect(route(req("/app/sw.js", { mode: "navigate" }), O).kind).toBe("network");
    expect(route(req("/app/assets/index-abc.js"), O).kind).toBe("static");
    expect(route(req("/app/pdfjs/cmaps/78-H.bcmap"), O).kind).toBe("static");
    expect(route(req("/app/sw.js"), O).kind).toBe("network");
  });
});

describe("parseRange", () => {
  it("handles the forms players and pdf.js send", () => {
    expect(parseRange(null, 1000)).toBeNull();
    expect(parseRange("bytes=0-99", 1000)).toEqual({ start: 0, end: 99 });
    expect(parseRange("bytes=900-", 1000)).toEqual({ start: 900, end: 999 });
    expect(parseRange("bytes=-100", 1000)).toEqual({ start: 900, end: 999 });
    expect(parseRange("bytes=-5000", 1000)).toEqual({ start: 0, end: 999 });
    expect(parseRange("bytes=990-2000", 1000)).toEqual({ start: 990, end: 999 });
  });
  it("refuses ranges outside the file and ignores what it cannot read", () => {
    expect(parseRange("bytes=1000-", 1000)).toBe("unsatisfiable");
    expect(parseRange("bytes=5-4", 1000)).toBe("unsatisfiable");
    expect(parseRange("bytes=-0", 1000)).toBe("unsatisfiable");
    expect(parseRange("bytes=0-1,5-6", 1000)).toBeNull();
    expect(parseRange("items=0-1", 1000)).toBeNull();
  });
});

function memoryQueue(results: SendResult[]) {
  const rows = new Map<string, QueuedPosition>();
  const sent: { e: QueuedPosition; keep: boolean }[] = [];
  const synced: [string, string | null][] = [];
  const q = new PositionQueue(
    { all: async () => [...rows.values()], put: async (e) => void rows.set(e.key, e), del: async (k) => void rows.delete(k) },
    async (e, { keep }) => {
      sent.push({ e, keep });
      return results.shift() ?? { ok: false, conflict: false, retry: true };
    },
    (e, seenAt) => synced.push([e.key, seenAt]),
    () => new Date("2026-10-21T12:00:00Z"),
  );
  return { q, rows, sent, synced };
}

const pos = (n: number, recordedAt = "2026-10-21T11:00:00Z") => ({ kind: "work" as const, id: 7, workId: 7, title: "Magi", body: { source: "web", sentence_n: n }, recordedAt, base: "2026-10-21T10:00:00Z" });

describe("offline position queue", () => {
  it("keeps only the newest position per book", async () => {
    const { q, rows } = memoryQueue([]);
    await q.add(pos(3));
    await q.add(pos(9, "2026-10-21T11:05:00Z"));
    expect([...rows.values()].map((e) => e.body.sentence_n)).toEqual([9]);
  });

  it("sends, then forgets what the server took and records when it saw it", async () => {
    const { q, rows, synced } = memoryQueue([{ ok: true, seenAt: "2026-10-21T12:00:01Z", server: null }]);
    await q.add(pos(9));
    expect(await q.flush()).toEqual({ synced: 1, conflicts: [], pending: 0 });
    expect(rows.size).toBe(0);
    expect(synced).toEqual([["work:7", "2026-10-21T12:00:01Z"]]);
  });

  it("holds a conflict for a decision and does not resend it on the next flush", async () => {
    const server = { device: "Kobo", received_at: "2026-10-21T11:30:00Z", percent: 0.4 };
    const { q, sent } = memoryQueue([{ ok: false, conflict: true, server, recordedAt: "2026-10-21T11:00:00Z" }]);
    await q.add(pos(9));
    const r = await q.flush();
    expect(r.conflicts).toHaveLength(1);
    expect(r.conflicts[0].conflict?.server).toEqual(server);
    await q.flush();
    expect(sent).toHaveLength(1);
  });

  it("'keep mine' resends with keep; 'theirs' drops it and adopts the server's time", async () => {
    const server = { device: "Kobo", received_at: "2026-10-21T11:30:00Z" };
    const a = memoryQueue([{ ok: false, conflict: true, server, recordedAt: null }, { ok: true, seenAt: "2026-10-21T12:00:02Z", server: null }]);
    await a.q.add(pos(9));
    await a.q.flush();
    await a.q.resolve("work:7", "mine");
    expect(a.sent.map((s) => s.keep)).toEqual([false, true]);
    expect(a.rows.size).toBe(0);

    const b = memoryQueue([{ ok: false, conflict: true, server, recordedAt: null }]);
    await b.q.add(pos(9));
    await b.q.flush();
    expect(await b.q.resolve("work:7", "theirs")).toEqual(server);
    expect(b.rows.size).toBe(0);
    expect(b.sent).toHaveLength(1);
    expect(b.synced).toEqual([["work:7", "2026-10-21T11:30:00Z"]]);
  });

  it("stops at the first network failure and keeps the rest, but drops what the server will never take", async () => {
    const { q, rows } = memoryQueue([{ ok: false, conflict: false, retry: false }, { ok: false, conflict: false, retry: true }]);
    await q.add(pos(1));
    await q.add({ ...pos(2), kind: "page", id: 3, body: { page: 2, pages: 9 } });
    const r = await q.flush();
    expect(rows.size).toBe(1);
    expect(r.pending).toBe(1);
  });
});

describe("install platform", () => {
  it("tells iPhone Safari, other iPhone browsers, iPad (which claims to be a Mac), and Android apart", () => {
    expect(platformOf("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")).toBe("ios-safari");
    expect(platformOf("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/130.0 Mobile/15E148 Safari/604.1")).toBe("ios-other");
    expect(platformOf("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15", 5)).toBe("ios-safari");
    expect(platformOf("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15", 0)).toBe("other");
    expect(platformOf("Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36")).toBe("android");
  });
});
