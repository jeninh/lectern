import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import { ApiError } from "../api";
import { planChunks, UploadManager, type Hasher, type Store, type Transport } from "../uploads";
import type { UploadSessionInfo } from "../types";

const hex = (b: Uint8Array) => createHash("sha256").update(b).digest("hex");

/** In-memory server that follows the contract's offset and checksum rules. */
class FakeServer implements Transport {
  sessions = new Map<string, { info: UploadSessionInfo; data: Uint8Array; sha?: string; idem: string }>();
  failNext: Array<"network" | "lost-response" | "503"> = [];
  chunkCalls = 0;
  constructor(public chunkSize = 4) {}
  async create(f: { name: string; size: number; type: string }, idem: string) {
    for (const s of this.sessions.values()) if (s.idem === idem) return { ...s.info };
    const id = `s${this.sessions.size + 1}`;
    const info: UploadSessionInfo = { id, filename: f.name, media_type: f.type, size: f.size, chunk_size: Math.min(this.chunkSize, f.size), received_bytes: 0, status: "active", error: null, created_at: null, expires_at: null, inbox_name: null };
    this.sessions.set(id, { info, data: new Uint8Array(0), idem });
    return { ...info };
  }
  async status(id: string) {
    const s = this.sessions.get(id);
    if (!s) throw new ApiError(404, "not_found", "not_found");
    return { ...s.info };
  }
  async chunk(id: string, offset: number, digest: string, body: Blob) {
    this.chunkCalls++;
    const s = this.sessions.get(id)!;
    const fault = this.failNext.shift();
    if (fault === "network") throw new TypeError("Failed to fetch");
    if (fault === "503") throw new ApiError(503, "busy");
    const bytes = new Uint8Array(await body.arrayBuffer());
    if (offset !== s.info.received_bytes) throw new ApiError(409, "offset_mismatch", "offset_mismatch", { received_bytes: s.info.received_bytes });
    if (hex(bytes) !== digest) throw new ApiError(422, "chunk_checksum", "chunk_checksum");
    const next = new Uint8Array(s.data.length + bytes.length);
    next.set(s.data);
    next.set(bytes, s.data.length);
    s.data = next;
    s.info.received_bytes = next.length;
    if (fault === "lost-response") throw new TypeError("connection reset after write");
    return s.info.received_bytes;
  }
  async finalize(id: string, sha256: string) {
    const s = this.sessions.get(id)!;
    if (s.info.status === "completed") return { title: "again" };
    if (s.info.received_bytes !== s.info.size) throw new ApiError(409, "incomplete", "incomplete");
    if (hex(s.data) !== sha256) throw new ApiError(422, "checksum_mismatch", "checksum_mismatch");
    s.info.status = "completed";
    return { title: "Done" };
  }
  async cancel(id: string) {
    this.sessions.get(id)!.info.status = "cancelled";
  }
}

/** Same contract as the worker: running whole-file hash, re-primed from zero when needed. */
function nodeHasher(): Hasher {
  const running = new Map<string, { h: ReturnType<typeof createHash>; pos: number }>();
  const bytes = async (f: Blob, a: number, b: number) => new Uint8Array(await f.slice(a, b).arrayBuffer());
  return {
    async chunk(key, file, start, end) {
      let r = running.get(key);
      if (!r || r.pos > start) running.set(key, (r = { h: createHash("sha256"), pos: 0 }));
      if (r.pos < start) r.h.update(await bytes(file, r.pos, start));
      const b = await bytes(file, start, end);
      r.h.update(b);
      r.pos = end;
      return hex(b);
    },
    async finish(key, file) {
      let r = running.get(key);
      if (!r || r.pos > file.size) running.set(key, (r = { h: createHash("sha256"), pos: 0 }));
      if (r.pos < file.size) r.h.update(await bytes(file, r.pos, file.size));
      running.delete(key);
      return r.h.digest("hex");
    },
    drop: (key) => void running.delete(key),
  };
}

function memStore(): Store & { m: Map<string, { id: string; idem: string }> } {
  const m = new Map<string, { id: string; idem: string }>();
  return { m, get: (k) => m.get(k), set: (k, v) => void m.set(k, v), delete: (k) => void m.delete(k) };
}

const file = (text: string, name = "book.epub") => Object.assign(new Blob([text]), { name, lastModified: 1 });
const settle = async (mgr: UploadManager) => {
  for (let i = 0; i < 200 && mgr.items.some((x) => !["done", "failed", "cancelled"].includes(x.state)); i++) await new Promise((r) => setTimeout(r, 1));
};

describe("planChunks", () => {
  it("covers the file exactly, from a resume point", () => {
    expect(planChunks(10, 4)).toEqual([[0, 4], [4, 8], [8, 10]]);
    expect(planChunks(10, 4, 4)).toEqual([[4, 8], [8, 10]]);
    expect(planChunks(8, 4, 8)).toEqual([]);
  });
});

describe("UploadManager", () => {
  it("survives dropped connections and lost responses and delivers the exact bytes", async () => {
    const server = new FakeServer(4);
    server.failNext = [undefined as never, "network", "lost-response", "503"];
    const mgr = new UploadManager(server, nodeHasher(), memStore(), async () => {});
    const f = file("The quick brown fox jumps");
    mgr.add([f]);
    await settle(mgr);
    const [item] = mgr.items;
    expect(item.state).toBe("done");
    expect(item.title).toBe("Done");
    const s = [...server.sessions.values()][0];
    expect(new TextDecoder().decode(s.data)).toBe("The quick brown fox jumps");
    expect(server.sessions.size).toBe(1); // resumed, never restarted
  });

  it("resumes a session left by an earlier page load, re-hashing the prefix locally", async () => {
    const server = new FakeServer(4);
    const store = memStore();
    const f = file("abcdefghijklmnop");
    const first = await server.create({ name: "book.epub", size: 16, type: "application/epub+zip" }, "idem-1");
    await server.chunk(first.id, 0, hex(new TextEncoder().encode("abcd")), new Blob(["abcd"]));
    store.set("book.epub:16:1", { id: first.id, idem: "idem-1" });
    const mgr = new UploadManager(server, nodeHasher(), store, async () => {});
    mgr.add([f]);
    await settle(mgr);
    expect(mgr.items[0].state).toBe("done");
    expect(server.chunkCalls).toBe(1 + 3); // only the missing chunks were sent
    expect(store.m.size).toBe(0);
  });

  it("gives up with a clear error when the server refuses, and rejects unsupported types", async () => {
    const server = new FakeServer(4);
    server.create = async () => {
      throw new ApiError(413, "That file is larger than this server accepts.", "too_large");
    };
    const mgr = new UploadManager(server, nodeHasher(), memStore(), async () => {});
    expect(mgr.add([file("x", "notes.pdf")])).toEqual(["notes.pdf"]);
    mgr.add([file("x".repeat(10))]);
    await settle(mgr);
    expect(mgr.items[0]).toMatchObject({ state: "failed", error: "That file is larger than this server accepts." });
  });

  it("cancels on the server", async () => {
    const server = new FakeServer(2);
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const hasher = nodeHasher();
    const slow: Hasher = { ...hasher, chunk: async (...a) => (await gate, hasher.chunk(...a)) };
    const mgr = new UploadManager(server, slow, memStore(), async () => {});
    mgr.add([file("abcdef")]);
    await new Promise((r) => setTimeout(r, 5));
    await mgr.cancel(mgr.items[0].key);
    release();
    await settle(mgr);
    expect(mgr.items[0].state).toBe("cancelled");
    expect([...server.sessions.values()][0].info.status).toBe("cancelled");
  });
});
