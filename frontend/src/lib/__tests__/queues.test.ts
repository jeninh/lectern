import { describe, expect, it } from "vitest";
import { groupDevices, moveItem, summarizeQueueResults } from "../queues";
import type { QueueDevice } from "../types";

const dev = (id: number, label: string, own: boolean, user = { id: own ? 1 : 2, username: own ? "owner" : "rex", display_name: own ? "" : "Rex" }): QueueDevice => ({
  id, device_id: `k${id}`, label, own, user, last_used_at: null, revoked: false, pending: 0,
});

describe("summarizeQueueResults", () => {
  const kobo = dev(1, "Kobo", true);
  it("counts sent and already waiting books together", () => {
    const r = summarizeQueueResults([
      { work_id: 1, status: "queued" }, { work_id: 2, status: "requeued" }, { work_id: 3, status: "already_queued" },
    ], kobo);
    expect(r).toEqual({ text: "Sent 2 books to Kobo · 1 already waiting", kind: "info" });
  });
  it("names someone else's device and explains what could not be sent", () => {
    const r = summarizeQueueResults([
      { work_id: 1, status: "queued" }, { work_id: 2, status: "not_visible_to_device" }, { work_id: 3, status: "no_ebook" },
    ], dev(4, "Boox", false));
    expect(r.text).toBe("Sent 1 book to Rex’s Boox · 1 book not shared with Rex · 1 book without an EPUB");
    expect(r.kind).toBe("info");
  });
  it("is an error only when nothing landed", () => {
    expect(summarizeQueueResults([{ work_id: 2, status: "no_ebook" }], kobo)).toEqual({ text: "1 book without an EPUB", kind: "error" });
    expect(summarizeQueueResults([{ work_id: 2, status: "already_queued" }], kobo)).toEqual({ text: "1 book already waiting on Kobo", kind: "info" });
  });
});

describe("moveItem", () => {
  it("moves up and down and clamps at the ends", () => {
    expect(moveItem([1, 2, 3], 2, -1)).toEqual([1, 3, 2]);
    expect(moveItem([1, 2, 3], 0, 1)).toEqual([2, 1, 3]);
    expect(moveItem([1, 2, 3], 0, -1)).toEqual([1, 2, 3]);
    expect(moveItem([1, 2, 3], 2, 5)).toEqual([1, 2, 3]);
  });
  it("never mutates its input", () => {
    const ids = [1, 2, 3];
    moveItem(ids, 1, 1);
    expect(ids).toEqual([1, 2, 3]);
  });
});

describe("groupDevices", () => {
  it("puts the caller's devices first, then one group per person", () => {
    const mia = { id: 3, username: "mia", display_name: "" };
    const groups = groupDevices([dev(5, "Boox", false), dev(1, "Kobo", true), dev(6, "Kindle", false, mia), dev(2, "Phone", true)]);
    expect(groups.map((g) => [g.label, g.devices.map((d) => d.label)])).toEqual([
      ["Your devices", ["Kobo", "Phone"]],
      ["Rex", ["Boox"]],
      ["mia", ["Kindle"]],
    ]);
  });
});
