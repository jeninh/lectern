import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import { ApiError } from "../api";
import { groupFiles, planChunks, UploadManager, type Hasher, type Store, type Transport } from "../uploads";
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
    expect(mgr.add([file("x", "saga.cbr")])).toEqual(["saga.cbr"]);
    mgr.add([file("x".repeat(10))]);
    await settle(mgr);
    expect(mgr.items[0]).toMatchObject({ state: "failed", error: "Too large for this server. Ask the owner to raise the upload limit." });
  });

  it("fails a file over the server's limit at once, without contacting the server", async () => {
    const server = new FakeServer(4);
    const mgr = new UploadManager(server, nodeHasher(), memStore(), async () => {});
    mgr.maxBytes = 1024;
    mgr.add([file("x".repeat(2048), "huge.m4b")]);
    await settle(mgr);
    expect(mgr.items[0].state).toBe("failed");
    expect(mgr.items[0].error).toMatch(/^Too large: 2\.0 KB\. This server accepts files up to 1\.0 KB\.$/);
    expect(server.sessions.size).toBe(0);
    mgr.retry(mgr.items[0].key); // still too large: retry is refused
    expect(mgr.items[0].state).toBe("failed");
  });

  it("pauses mid-file and resumes from the server's offset without resending", async () => {
    const server = new FakeServer(2);
    let gate: (() => void) | null = null;
    const hasher = nodeHasher();
    let calls = 0;
    const slow: Hasher = {
      ...hasher,
      chunk: async (...a) => {
        if (++calls === 2) await new Promise<void>((r) => (gate = r));
        return hasher.chunk(...a);
      },
    };
    const mgr = new UploadManager(server, slow, memStore(), async () => {});
    mgr.add([file("abcdefgh")]);
    for (let i = 0; i < 50 && !gate; i++) await new Promise((r) => setTimeout(r, 1));
    const key = mgr.items[0].key;
    mgr.pause(key);
    expect(mgr.items[0].state).toBe("paused");
    gate!();
    await new Promise((r) => setTimeout(r, 5));
    expect(mgr.items[0].state).toBe("paused");
    const sentBefore = [...server.sessions.values()][0].info.received_bytes;
    expect(sentBefore).toBe(2);
    mgr.resume(key);
    await settle(mgr);
    expect(mgr.items[0].state).toBe("done");
    expect(new TextDecoder().decode([...server.sessions.values()][0].data)).toBe("abcdefgh");
    expect(server.sessions.size).toBe(1);
    expect(server.chunkCalls).toBe(4); // every chunk exactly once
  });

  it("retries a failed file in place", async () => {
    const server = new FakeServer(4);
    let refuse = true;
    const create = server.create.bind(server);
    server.create = async (...a) => {
      if (refuse) throw new ApiError(422, "x", "not_epub");
      return create(...a);
    };
    const mgr = new UploadManager(server, nodeHasher(), memStore(), async () => {});
    mgr.add([file("abcdef")]);
    await settle(mgr);
    expect(mgr.items[0]).toMatchObject({ state: "failed", error: expect.stringMatching(/^Wrong format/) });
    refuse = false;
    mgr.retry(mgr.items[0].key);
    await settle(mgr);
    expect(mgr.items[0].state).toBe("done");
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

/** Adds the review-before-filing calls to the fake server. */
class ImportServer extends FakeServer {
  imports = new Map<string, { idem: string; files: string[]; completed: number; discarded: boolean }>();
  dupNames = new Set<string>();
  sessionImport = new Map<string, string | undefined>();
  async create(f: { name: string; size: number; type: string }, idem: string, importId?: string) {
    const s = await super.create(f, idem);
    this.sessionImport.set(s.id, importId);
    return s;
  }
  async finalize(id: string, sha256: string) {
    const r = await super.finalize(id, sha256);
    const s = this.sessions.get(id)!;
    const imp = this.sessionImport.get(id);
    if (imp) this.imports.get(imp)!.files.push(s.info.filename);
    const dup = this.dupNames.has(s.info.filename);
    return { ...r, file: { id: 1, status: dup ? ("duplicate" as const) : ("ok" as const), duplicate_work_id: dup ? 7 : null } };
  }
  async createImport(_label: string, idem: string) {
    for (const [id, v] of this.imports) if (v.idem === idem) return { id } as never;
    const id = `i${this.imports.size + 1}`;
    this.imports.set(id, { idem, files: [], completed: 0, discarded: false });
    return { id } as never;
  }
  async completeImport(id: string) {
    this.imports.get(id)!.completed++;
    return { id, status: "ready", label: "", files: [] } as never;
  }
  async discardImport(id: string) {
    this.imports.get(id)!.discarded = true;
  }
}

describe("UploadManager with imports", () => {
  it("uploads a book's files into one import and hands it to review once, after the last file", async () => {
    const server = new ImportServer(4);
    server.dupNames.add("02.mp3");
    const mgr = new UploadManager(server, nodeHasher(), memStore(), async () => {});
    const ready: string[] = [];
    mgr.onImportReady = (info) => ready.push(info.id);
    mgr.add([file("track one", "01.mp3"), file("track two", "02.mp3"), file("track three", "03.mp3")], { label: "Book" });
    await settle(mgr);
    await new Promise((r) => setTimeout(r, 2));
    expect(server.imports.size).toBe(1);
    expect(server.imports.get("i1")).toMatchObject({ files: ["01.mp3", "02.mp3", "03.mp3"], completed: 1 });
    expect(ready).toEqual(["i1"]);
    expect(mgr.items.find((i) => i.name === "02.mp3")?.duplicateOf).toEqual({ work_id: 7 });
    expect(mgr.items.find((i) => i.name === "01.mp3")?.duplicateOf).toBeNull();
  });

  it("uploads same-named tracks from different disc folders as separate files", async () => {
    const server = new ImportServer(4);
    const mgr = new UploadManager(server, nodeHasher(), memStore(), async () => {});
    const disc = (n: number) => Object.assign(new Blob([`disc ${n} track`]), { name: "01.mp3", lastModified: 1, relPath: `Dune/CD ${n}/01.mp3` });
    mgr.add([disc(1), disc(2)], { label: "Dune" });
    await settle(mgr);
    expect(mgr.items.map((i) => i.state)).toEqual(["done", "done"]);
    expect([...server.sessions.values()].map((s) => new TextDecoder().decode(s.data))).toEqual(["disc 1 track", "disc 2 track"]);
  });

  it("discards the import when every file of the book is cancelled", async () => {
    const server = new ImportServer(2);
    let gate!: () => void;
    const g = new Promise<void>((r) => (gate = r));
    const hasher = nodeHasher();
    const slow: Hasher = { ...hasher, chunk: async (...a) => (await g, hasher.chunk(...a)) };
    const mgr = new UploadManager(server, slow, memStore(), async () => {});
    const ready: string[] = [];
    mgr.onImportReady = (i) => ready.push(i.id);
    mgr.add([file("abcdef", "a.mp3"), file("ghijkl", "b.mp3")], { label: "Book" });
    await new Promise((r) => setTimeout(r, 5));
    for (const it of [...mgr.items]) await mgr.cancel(it.key);
    gate();
    await settle(mgr);
    await new Promise((r) => setTimeout(r, 2));
    expect(ready).toEqual([]);
    expect(server.imports.get("i1")?.discarded).toBe(true);
  });

  it("files the rest of a book when one of its files fails, and a later retry starts a new import", async () => {
    const server = new ImportServer(4);
    const create = server.create.bind(server);
    let refuse = true;
    server.create = async (f, idem, imp) => {
      if (f.name === "bad.epub" && refuse) throw new ApiError(413, "x", "too_large");
      return create(f, idem, imp);
    };
    const mgr = new UploadManager(server, nodeHasher(), memStore(), async () => {});
    const ready: string[] = [];
    mgr.onImportReady = (i) => ready.push(i.id);
    mgr.add([file("good one", "good.mp3"), file("bad", "bad.epub")], { label: "Book" });
    await settle(mgr);
    await new Promise((r) => setTimeout(r, 2));
    expect(ready).toEqual(["i1"]);
    refuse = false;
    mgr.retry(mgr.items.find((i) => i.name === "bad.epub")!.key);
    await settle(mgr);
    await new Promise((r) => setTimeout(r, 2));
    expect(ready).toEqual(["i1", "i2"]);
    expect(server.imports.get("i2")?.files).toEqual(["bad.epub"]);
  });
});

describe("groupFiles", () => {
  const f = (name: string, relPath?: string) => Object.assign(new Blob(["x"]), { name, lastModified: 1, relPath });
  const shape = (files: ReturnType<typeof f>[]) => groupFiles(files).map((g) => [g.label, g.files.map((x) => x.name)]);

  it("makes each loose EPUB and M4B its own book, and loose tracks dropped together one audiobook", () => {
    expect(shape([f("A.epub"), f("B.m4b"), f("02.mp3"), f("10.mp3"), f("01.mp3")])).toEqual([
      ["3 audio files", ["01.mp3", "02.mp3", "10.mp3"]],
      ["A", ["A.epub"]],
      ["B", ["B.m4b"]],
    ]);
  });

  it("makes one audiobook per folder, merges CD subfolders, and keeps a folder's single EPUB with it", () => {
    expect(
      shape([
        f("01.mp3", "Dune/CD 2/01.mp3"),
        f("01.mp3", "Dune/CD 1/01.mp3"),
        f("Dune.epub", "Dune/Dune.epub"),
        f("1.mp3", "Emma/1.mp3"),
        f("x.epub", "Shelf/x.epub"),
        f("y.epub", "Shelf/y.epub"),
      ]),
    ).toEqual([
      ["Dune", ["Dune.epub", "01.mp3", "01.mp3"]],
      ["Emma", ["1.mp3"]],
      ["x", ["x.epub"]],
      ["y", ["y.epub"]],
    ]);
    const dune = groupFiles([f("01.mp3", "Dune/CD 2/01.mp3"), f("01.mp3", "Dune/CD 1/01.mp3")])[0];
    expect(dune.files.map((x) => x.relPath)).toEqual(["Dune/CD 1/01.mp3", "Dune/CD 2/01.mp3"]);
  });
});

