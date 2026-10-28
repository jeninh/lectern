/** The job queue page (GET /api/job-queue, issue #87). */
import type { Blocked } from "./limits";

export type JobGroup = "running" | "waiting" | "paused" | "failed" | "done";
export const GROUPS: JobGroup[] = ["running", "waiting", "paused", "failed", "done"];
export const GROUP_LABEL: Record<JobGroup, string> = {
  running: "Running",
  waiting: "Waiting",
  paused: "Paused",
  failed: "Failed",
  done: "Done (recent)",
};

export interface QueueRow {
  id: number;
  kind: string;
  detail: string | null;
  capability: string;
  work_id: number | null;
  /** null when the caller may not see the book (or the job has none). */
  title: string | null;
  status: "queued" | "leased" | "succeeded" | "failed" | "cancelled";
  group: JobGroup;
  priority: number;
  runs_on: string | null;
  progress: number | null;
  waiting: string | null;
  /** Set while a per-feature or the global heavy-job cap holds this job back (#88). */
  limit: Blocked;
  feature: string | null;
  ahead: number | null;
  attempts: number;
  max_attempts: number;
  error: string | null;
  batch: string | null;
  mine: boolean;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface QueueBatch {
  id: string;
  kind: string;
  group: JobGroup;
  counts: Record<JobGroup, number>;
  total: number;
  created_at: string | null;
  mine: boolean;
  jobs: QueueRow[];
}

export interface JobQueue {
  queue_paused: boolean;
  can_manage: boolean;
  jobs: QueueRow[];
  batches: QueueBatch[];
}

const KIND: Record<string, string> = {
  align: "Read-along alignment",
  transcode: "Data-saver audio",
  ocr: "OCR",
  pdf2epub: "Convert to EPUB",
  article: "Save article",
  "storage-cleanup": "Storage cleanup",
};
const ENGINE: Record<string, string> = { kokoro: "Kokoro", piper: "Piper", elevenlabs: "ElevenLabs" };

/** What the job does, e.g. "Audiobook (Kokoro)", "PDF analysis". */
export function kindLabel(j: Pick<QueueRow, "kind" | "detail">): string {
  if (j.kind === "tts") return j.detail ? `Audiobook (${ENGINE[j.detail] ?? j.detail})` : "Audiobook";
  if (j.kind === "pdf2epub" && j.detail === "analyse") return "PDF analysis";
  if (j.kind === "align" && j.detail) return `${KIND.align} (${j.detail})`;
  return KIND[j.kind] ?? j.kind;
}

/** The book, or a neutral stand-in when the caller may not see it. */
export function bookLabel(j: Pick<QueueRow, "title" | "work_id" | "kind">): string {
  if (j.title) return j.title;
  if (j.kind === "article") return "Web article";
  if (j.work_id == null) return "Library";
  return "A book you can’t see";
}

/** Where the job stands, in one line: "waiting: limit reached" while a #88 cap holds it. */
export function stateLabel(j: QueueRow): string {
  if (j.group === "waiting" && (j.limit === "feature_limit" || j.limit === "global_limit")) return "Waiting: limit reached";
  if (j.group === "running") {
    const pct = j.progress == null ? "" : ` · ${Math.round(j.progress * 100)} %`;
    return `Running on ${j.runs_on ?? "this server"}${pct}`;
  }
  if (j.group === "failed") return j.error ? `Failed: ${j.error.split("\n")[0].slice(0, 200)}` : "Failed";
  if (j.group === "done") return j.status === "cancelled" ? "Cancelled" : "Done";
  return j.waiting ?? (j.group === "paused" ? "Paused" : "Waiting");
}

/** "2 of 5 done · 1 failed · 2 waiting" */
export function batchSummary(b: QueueBatch): string {
  const parts = [`${b.counts.done} of ${b.total} done`];
  for (const g of ["running", "failed", "waiting", "paused"] as const) {
    if (b.counts[g]) parts.push(`${b.counts[g]} ${g}`);
  }
  return parts.join(" · ");
}

export type QueueItem = { type: "job"; job: QueueRow } | { type: "batch"; batch: QueueBatch };

/** Jobs and batches by group, in page order; a batch sits in the group of its liveliest job. */
export function groupItems(q: JobQueue): Record<JobGroup, QueueItem[]> {
  const out = Object.fromEntries(GROUPS.map((g) => [g, [] as QueueItem[]])) as Record<JobGroup, QueueItem[]>;
  for (const b of q.batches) out[b.group].push({ type: "batch", batch: b });
  for (const j of q.jobs) out[j.group].push({ type: "job", job: j });
  return out;
}
