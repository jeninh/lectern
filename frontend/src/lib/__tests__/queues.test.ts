import { describe, expect, it } from "vitest";
import { followOptions, groupDevices, moveItem, shelfSyncChips, summarizeQueueResults } from "../queues";
import type { QueueDevice, Shelf } from "../types";

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
    expect(r.text).toBe("Sent 1 book to Rex’s Boox · 1 book not shared with Rex · 1 book without an EPUB, PDF, or comic");
    expect(r.kind).toBe("info");
  });
  it("is an error only when nothing landed", () => {
    expect(summarizeQueueResults([{ work_id: 2, status: "no_ebook" }], kobo)).toEqual({ text: "1 book without an EPUB, PDF, or comic", kind: "error" });
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

describe("shelf sync", () => {
  const shelf = (id: number, path: string[], smart = false): Shelf => ({
    id, name: path[path.length - 1], parent_id: null, path, owner: "owner", owner_id: 1, permission: "view",
    work_count: 0, shelf_count: 0, grant_count: 0, smart,
  });
  const shelves = [shelf(1, ["Fiction"]), shelf(2, ["Fiction", "Sci-fi"]), shelf(3, ["New", "This month"], true)];

  it("offers every shelf by its path, nested and smart ones included", () => {
    expect(followOptions(shelves)).toEqual([
      { id: 1, label: "Fiction" },
      { id: 2, label: "Fiction / Sci-fi" },
      { id: 3, label: "New / This month (smart)" },
    ]);
  });
  it("shows the followed shelf by name, its path and clean-up on hover, and auto-download", () => {
    expect(shelfSyncChips({ follow_shelf_id: 2, follow_cleanup: true, auto_download: true }, shelves)).toEqual([
      { text: "Follows Sci-fi", title: "Follows Fiction / Sci-fi; books that leave it are removed" },
      { text: "Auto-download", title: "The KOReader plugin downloads queued books by itself" },
    ]);
    expect(shelfSyncChips({ follow_shelf_id: 1, follow_cleanup: false, auto_download: false }, shelves)).toEqual([{ text: "Follows Fiction", title: "Follows Fiction" }]);
    expect(shelfSyncChips({ follow_shelf_id: null, follow_cleanup: true, auto_download: false }, shelves)).toEqual([]);
    expect(shelfSyncChips(null, shelves)).toEqual([]);
  });
  it("never names a followed shelf this person can no longer see", () => {
    expect(shelfSyncChips({ follow_shelf_id: 9, follow_cleanup: false, auto_download: false }, shelves)).toEqual([{ text: "Follows a hidden shelf", title: "Follows a shelf you can’t see" }]);
  });
});
