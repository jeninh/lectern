import { describe, expect, it } from "vitest";
import {
  canConfirm,
  categoryRows,
  type CleanupJob,
  confirmLabel,
  freeTrend,
  jobActive,
  jobText,
  preRestoreDate,
  type ScanResult,
  selection,
} from "../storage";

const MB = 1024 * 1024;

const cat = (bytes: number, files = 1) => ({ bytes, files, parts: {}, volumes: {} });
const scan: ScanResult = {
  scanned_at: "2026-10-28T10:00:00Z",
  duration_s: 1.2,
  categories: {
    originals: cat(600 * MB, 40),
    generated_audio: cat(300 * MB, 12),
    ocr: cat(0, 0),
    converted: cat(20 * MB),
    covers: cat(5 * MB),
    derived_text: cat(25 * MB),
    trash: cat(40 * MB),
    backups: cat(0, 0),
    other: cat(10 * MB),
  },
  total_bytes: 1000 * MB,
  largest_works: [],
  pre_restore: [],
  skipped_links: 0,
};

describe("categoryRows", () => {
  it("orders categories by size with their share of the total, and keeps empty ones", () => {
    const rows = categoryRows(scan);
    expect(rows.map((r) => r.key).slice(0, 3)).toEqual(["originals", "generated_audio", "trash"]);
    expect(rows[0].share).toBeCloseTo(0.6);
    expect(rows.find((r) => r.key === "ocr")?.bytes).toBe(0);
    expect(rows).toHaveLength(9);
  });
});

describe("freeTrend", () => {
  const sample = (at: string, free: number) => ({ at, volumes: { data: { free, total: 32_000 * MB } } });
  it("is the change in free space per day between the first and last sample", () => {
    const h = [sample("2026-10-20T00:00:00Z", 1000 * MB), sample("2026-10-22T00:00:00Z", 900 * MB), sample("2026-10-24T00:00:00Z", 800 * MB)];
    expect(freeTrend(h, "data")).toBeCloseTo(-50 * MB);
  });
  it("needs a day or more of history on that volume", () => {
    expect(freeTrend([sample("2026-10-20T00:00:00Z", 1)], "data")).toBeNull();
    expect(freeTrend([sample("2026-10-20T00:00:00Z", 1), sample("2026-10-20T12:00:00Z", 2)], "data")).toBeNull();
    expect(freeTrend([sample("2026-10-20T00:00:00Z", 1), sample("2026-10-23T00:00:00Z", 2)], "backups")).toBeNull();
  });
});

describe("confirmation", () => {
  const items = [
    { key: "work:1", label: "A", bytes: 3 * MB, files: 2 },
    { key: "work:2", label: "B", bytes: 1 * MB, files: 1 },
    { key: "work:3", label: "C", bytes: 6 * MB, files: 4 },
  ];
  it("totals only what is still selected", () => {
    expect(selection(items, new Set(["work:1", "work:3"]))).toEqual({ count: 2, files: 6, bytes: 9 * MB });
    expect(selection(items, new Set())).toEqual({ count: 0, files: 0, bytes: 0 });
  });
  it("needs a selection and the box ticked, and never while a cleanup runs or is being sent", () => {
    const ok = { selected: 2, acknowledged: true, running: false, sending: false };
    expect(canConfirm(ok)).toBe(true);
    expect(canConfirm({ ...ok, acknowledged: false })).toBe(false);
    expect(canConfirm({ ...ok, selected: 0 })).toBe(false);
    expect(canConfirm({ ...ok, running: true })).toBe(false);
    expect(canConfirm({ ...ok, sending: true })).toBe(false);
  });
  it("names the action, the count, and the space on the button", () => {
    expect(confirmLabel("trash", { count: 1, bytes: 1.5 * MB })).toBe("Delete for good: 1 item, 1.5 MB");
    expect(confirmLabel("covers", { count: 3, bytes: 2048 })).toBe("Delete covers: 3 items, 2 KB");
  });
});

describe("jobText", () => {
  const job = (over: Partial<CleanupJob>): CleanupJob => ({
    id: 1, action: "renditions", status: "succeeded", total: 4, done: 4, skipped: 1, expected_bytes: 9 * MB,
    freed_bytes: 8 * MB, errors: [], error: null, run_after: null, created_at: "", finished_at: null, ...over,
  });
  it("reports what was removed, skipped, and freed", () => {
    expect(jobText(job({}))).toBe("Data-saver copies: 3 removed, 1 skipped (changed since the preview); 8.0 MB freed.");
  });
  it("says why a queued cleanup waits", () => {
    expect(jobText(job({ status: "queued", error: "Waiting for the backup to finish." }))).toBe("Data-saver copies: Waiting for the backup to finish.");
    expect(jobActive(job({ status: "queued" }))).toBe(true);
    expect(jobActive(job({ status: "succeeded" }))).toBe(false);
    expect(jobActive(null)).toBe(false);
  });
});

describe("preRestoreDate", () => {
  it("reads the time from the folder name and refuses anything else", () => {
    expect(preRestoreDate("pre-restore-20260101T020304Z")).toBe("2026-01-01T02:03:04Z");
    expect(preRestoreDate("pre-restore-latest")).toBeNull();
    expect(preRestoreDate("../pre-restore-20260101T020304Z")).toBeNull();
  });
});
