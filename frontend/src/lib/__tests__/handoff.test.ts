import { describe, expect, it } from "vitest";
import { pickHandoff } from "../handoff";
import type { WorkProgressResponse } from "../types";

const base: WorkProgressResponse = {
  work_id: 1, position: { sentence_n: 100, offset: 0 }, source: "web", raw: "100:0", device: "Phone",
  received_at: "2026-10-20T10:00:00Z", text: { spine_index: 3, char: 400 }, audio_s: 600, percent: 0.4,
  estimated: false, count: 250, total_duration_s: 1500, koreader: null,
};
const kindle = { source: "koreader" as const, device: "Kindle", received_at: "2026-10-20T10:12:00Z", percent: 0.55, raw: "/body/DocFragment[4]", text: { spine_index: 3, char: 900 }, audio_s: 825, estimated: true };

describe("pickHandoff", () => {
  it("offers a newer position from another device", () => {
    const c = pickHandoff(base, { seenAt: "2026-10-20T09:00:00Z" }, { device: "Laptop", mode: "read", here: 0.1 });
    expect(c?.device).toBe("Phone");
  });

  it("stays quiet about our own write and anything we already saw", () => {
    expect(pickHandoff(base, { seenAt: null }, { device: "Phone", mode: "read", here: 0.1 })).toBeNull();
    expect(pickHandoff(base, { seenAt: "2026-10-20T10:00:00Z" }, { device: "Laptop", mode: "read", here: 0.1 })).toBeNull();
  });

  it("prefers the newest source, including KOReader", () => {
    const c = pickHandoff({ ...base, koreader: kindle }, { seenAt: "2026-10-20T09:00:00Z" }, { device: "Laptop", mode: "read", here: 0.4 });
    expect(c?.device).toBe("Kindle");
  });

  it("ignores moves too small to be worth a prompt", () => {
    expect(pickHandoff(base, { seenAt: null }, { device: "Laptop", mode: "read", here: 0.402 })).toBeNull();
    expect(pickHandoff(base, { seenAt: null }, { device: "Laptop", mode: "listen", here: 590 })).toBeNull();
    expect(pickHandoff(base, { seenAt: null }, { device: "Laptop", mode: "listen", here: 100 })?.audio_s).toBe(600);
  });

  it("needs a target it can use: a listener cannot jump to a text-only position", () => {
    const textOnly = { ...base, audio_s: null };
    expect(pickHandoff(textOnly, { seenAt: null }, { device: "Laptop", mode: "listen", here: 0 })).toBeNull();
  });
});
