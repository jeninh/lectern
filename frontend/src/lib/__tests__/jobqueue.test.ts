import { describe, expect, it } from "vitest";
import { batchSummary, bookLabel, groupItems, kindLabel, stateLabel, type JobQueue, type QueueBatch, type QueueRow } from "../jobqueue";

const row = (over: Partial<QueueRow> = {}): QueueRow => ({
  id: 1,
  kind: "pdf2epub",
  detail: "convert",
  capability: "pdf2epub",
  work_id: 7,
  title: "Middlemarch",
  status: "queued",
  group: "waiting",
  priority: 30,
  runs_on: null,
  progress: null,
  waiting: "Next for this server",
  limit: null,
  feature: "convert",
  ahead: 0,
  attempts: 0,
  max_attempts: 3,
  error: null,
  batch: null,
  mine: true,
  created_at: null,
  started_at: null,
  finished_at: null,
  ...over,
});

const batch = (counts: Partial<QueueBatch["counts"]>, group: QueueBatch["group"] = "waiting"): QueueBatch => {
  const c = { running: 0, waiting: 0, paused: 0, failed: 0, done: 0, ...counts };
  return { id: "0123456789abcdef", kind: "pdf2epub", group, counts: c, total: Object.values(c).reduce((a, b) => a + b, 0), created_at: null, mine: true, jobs: [] };
};

describe("job queue", () => {
  it("says a job held back by a resource limit is waiting: limit reached", () => {
    expect(stateLabel(row({ limit: "feature_limit" }))).toBe("Waiting: limit reached");
    expect(stateLabel(row({ limit: "global_limit" }))).toBe("Waiting: limit reached");
    // a feature off on this server is not a limit: the job waits for a worker
    expect(stateLabel(row({ limit: "off", waiting: "Waiting for a worker that can run ocr" }))).toBe("Waiting for a worker that can run ocr");
    // only waiting jobs: a paused job is held by the owner
    expect(stateLabel(row({ group: "paused", limit: "feature_limit", waiting: "Paused" }))).toBe("Paused");
  });

  it("describes running, failed, and finished jobs", () => {
    expect(stateLabel(row({ group: "running", status: "leased", runs_on: "mac", progress: 0.426 }))).toBe("Running on mac · 43 %");
    expect(stateLabel(row({ group: "running", status: "leased", runs_on: "this server" }))).toBe("Running on this server");
    expect(stateLabel(row({ group: "failed", status: "failed", error: "out of memory\nTraceback …" }))).toBe("Failed: out of memory");
    expect(stateLabel(row({ group: "failed", status: "failed", error: null }))).toBe("Failed");
    expect(stateLabel(row({ group: "done", status: "cancelled" }))).toBe("Cancelled");
  });

  it("names kinds and never invents a title the caller cannot see", () => {
    expect(kindLabel({ kind: "tts", detail: "kokoro" })).toBe("Audiobook (Kokoro)");
    expect(kindLabel({ kind: "pdf2epub", detail: "analyse" })).toBe("PDF analysis");
    expect(kindLabel({ kind: "something-new", detail: null })).toBe("something-new");
    expect(bookLabel(row({ title: null }))).toBe("A book you can’t see");
    expect(bookLabel(row({ title: null, work_id: null, kind: "storage-cleanup" }))).toBe("Library");
  });

  it("summarises a batch and files it under its liveliest group", () => {
    expect(batchSummary(batch({ done: 2, running: 1, failed: 1, waiting: 3 }))).toBe("2 of 7 done · 1 running · 1 failed · 3 waiting");
    const q: JobQueue = {
      queue_paused: false,
      can_manage: true,
      jobs: [row({ id: 1, group: "failed" }), row({ id: 2 })],
      batches: [batch({ paused: 2, done: 1 }, "paused")],
    };
    const g = groupItems(q);
    expect(g.paused.map((i) => i.type)).toEqual(["batch"]);
    expect(g.failed.map((i) => (i.type === "job" ? i.job.id : null))).toEqual([1]);
    expect(g.waiting).toHaveLength(1);
    expect(g.running).toHaveLength(0);
  });
});
