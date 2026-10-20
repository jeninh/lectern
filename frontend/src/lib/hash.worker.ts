/// <reference lib="webworker" />
// SHA-256 off the main thread for resumable uploads. Web Crypto cannot hash incrementally, so
// the whole-file digest uses @noble/hashes and is fed chunk by chunk as the upload proceeds.
import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

type Running = { h: ReturnType<typeof sha256.create>; pos: number };
const running = new Map<string, Running>();
const READ = 8 * 1024 * 1024;

async function feed(r: Running, file: Blob, upTo: number) {
  while (r.pos < upTo) {
    const end = Math.min(upTo, r.pos + READ);
    r.h.update(new Uint8Array(await file.slice(r.pos, end).arrayBuffer()));
    r.pos = end;
  }
}

async function chunkDigest(bytes: Uint8Array): Promise<string> {
  if (self.crypto?.subtle) {
    return bytesToHex(new Uint8Array(await self.crypto.subtle.digest("SHA-256", bytes as Uint8Array<ArrayBuffer>)));
  }
  return bytesToHex(sha256(bytes));
}

export type HashRequest =
  | { id: number; op: "chunk"; key: string; file: Blob; start: number; end: number }
  | { id: number; op: "finish"; key: string; file: Blob }
  | { id: number; op: "drop"; key: string };

self.onmessage = async (e: MessageEvent<HashRequest>) => {
  const msg = e.data;
  try {
    if (msg.op === "drop") {
      running.delete(msg.key);
      self.postMessage({ id: msg.id, hex: "" });
      return;
    }
    let r = running.get(msg.key);
    const start = msg.op === "chunk" ? msg.start : msg.file.size;
    if (!r || r.pos > start) {
      // First use, or the server has fewer bytes than we hashed: start over from zero.
      r = { h: sha256.create(), pos: 0 };
      running.set(msg.key, r);
    }
    await feed(r, msg.file, start); // catch up after a resume
    if (msg.op === "chunk") {
      const bytes = new Uint8Array(await msg.file.slice(msg.start, msg.end).arrayBuffer());
      r.h.update(bytes);
      r.pos = msg.end;
      self.postMessage({ id: msg.id, hex: await chunkDigest(bytes) });
    } else {
      const hex = bytesToHex(r.h.digest());
      running.delete(msg.key);
      self.postMessage({ id: msg.id, hex });
    }
  } catch (err) {
    self.postMessage({ id: msg.id, error: String(err) });
  }
};
