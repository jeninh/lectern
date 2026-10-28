// The storage dashboard (Admin → Storage, docs/storage.md): what GET /api/admin/storage returns,
// and the decisions the card makes from it. Kept free of React so the tests can check them.
import { bytes } from "./format";

export type Category = "originals" | "generated_audio" | "ocr" | "converted" | "covers" | "derived_text" | "trash" | "backups" | "other";
export type Action = "trash" | "renditions" | "covers" | "read_aloud" | "leftovers" | "pre_restore";

export interface Volume {
  id: string;
  roles: string[];
  paths: string[];
  total: number;
  used: number;
  free: number;
  free_percent: number;
  low: boolean;
}

export interface CategoryTotal {
  bytes: number;
  files: number;
  parts: Record<string, number>;
  volumes: Record<string, number>;
}

export interface ScanResult {
  scanned_at: string;
  duration_s: number;
  categories: Record<Category, CategoryTotal>;
  total_bytes: number;
  largest_works: { id: number; title: string; author: string; trashed: boolean; bytes: number; categories: Partial<Record<Category, number>> }[];
  pre_restore: { name: string; bytes: number }[];
  skipped_links: number;
}

export interface HistorySample {
  at: string;
  volumes: Record<string, { free: number; total: number }>;
}

export interface CleanupJob {
  id: number;
  action: Action;
  status: "queued" | "leased" | "succeeded" | "failed" | "cancelled";
  total: number;
  done: number;
  skipped: number;
  expected_bytes: number;
  freed_bytes: number;
  errors: string[];
  error: string | null;
  run_after: string | null;
  created_at: string;
  finished_at: string | null;
}

export type Blocked = "backup_running" | "restore_interrupted" | null;

export interface StorageView {
  volumes: Volume[];
  warn_free_percent: number;
  scan: ScanResult | null;
  history: HistorySample[];
  scanning: boolean;
  blocked: Blocked;
  job: CleanupJob | null;
}

export interface PreviewItem {
  key: string;
  label: string;
  bytes: number;
  files: number;
  work_id?: number;
  created_at?: string | null;
}

export interface Preview {
  action: Action;
  params: { older_than_days?: number; work_id?: number; name?: string };
  items: PreviewItem[];
  skipped: { work_id: number; label: string; reason: string }[];
  total_bytes: number;
  total_files: number;
  blocked: Blocked;
}

export const CATEGORIES: { key: Category; label: string; hint: string }[] = [
  { key: "originals", label: "Originals", hint: "The books and audiobooks you added. Never removed from this page." },
  { key: "generated_audio", label: "Generated audio", hint: "Narrated audiobooks and data-saver copies of audiobooks." },
  { key: "ocr", label: "OCR copies", hint: "Searchable copies of scanned PDFs and their recognised words." },
  { key: "converted", label: "Converted editions", hint: "PDF to EPUB editions, their reports, and read-aloud EPUBs." },
  { key: "covers", label: "Covers", hint: "Generated covers and the cover backups Undo keeps." },
  { key: "derived_text", label: "Derived text", hint: "PDF text documents, sentence maps, the search index." },
  { key: "trash", label: "Trash", hint: "Removed books and files, kept for 30 days." },
  { key: "backups", label: "Backups", hint: "The restic repository, leftover staging, and pre-restore folders." },
  { key: "other", label: "Other", hint: "The database, upload parts, logs, the inbox, and scratch space." },
];

export const ACTIONS: Record<Action, { title: string; verb: string; describe: string }> = {
  trash: { title: "Empty the Trash", verb: "Delete for good", describe: "Books and files in the Trash are deleted for good. They cannot be restored afterwards." },
  renditions: { title: "Data-saver copies", verb: "Delete copies", describe: "The smaller copies of audiobooks. The originals stay; copies can be made again from Admin → Data-saver copies or the book's page." },
  covers: { title: "Generated covers", verb: "Delete covers", describe: "Covers drawn for books without cover art. They are drawn again the next time the book is shown." },
  read_aloud: { title: "Read-aloud EPUBs", verb: "Delete EPUBs", describe: "EPUBs with the narration built in. They are built again when someone downloads one." },
  leftovers: { title: "Leftovers of interrupted runs", verb: "Delete leftovers", describe: "Upload parts nobody is sending any more, staged results and scratch files of finished or killed jobs, and backup staging left by a killed backup." },
  pre_restore: { title: "Pre-restore folder", verb: "Delete folder", describe: "The files a restore replaced, kept in case the restore was a mistake. Delete one only once you are sure the restore was right." },
};

export const BLOCKED_TEXT: Record<Exclude<Blocked, null>, string> = {
  backup_running: "A backup is running. Cleanup waits until it ends; nothing is deleted meanwhile.",
  restore_interrupted: "A restore was interrupted. Finish or undo it (lectern restore --resume or --undo) before anything is cleaned up.",
};

/** Category rows, largest first, each with its share of everything scanned. */
export function categoryRows(scan: ScanResult): { key: Category; label: string; hint: string; bytes: number; files: number; share: number }[] {
  const total = scan.total_bytes || 1;
  return CATEGORIES.map((c) => {
    const t = scan.categories[c.key] ?? { bytes: 0, files: 0 };
    return { ...c, bytes: t.bytes, files: t.files, share: t.bytes / total };
  }).sort((a, b) => b.bytes - a.bytes);
}

/** Change in free space per day on one volume over the history (negative: filling up), or null
 * with fewer than two samples a day or more apart. */
export function freeTrend(history: HistorySample[], volume: string): number | null {
  const points = history.filter((h) => h.volumes[volume]).map((h) => ({ t: Date.parse(h.at), free: h.volumes[volume].free }));
  if (points.length < 2) return null;
  const first = points[0];
  const last = points[points.length - 1];
  const days = (last.t - first.t) / 86_400_000;
  if (!(days >= 1)) return null;
  return (last.free - first.free) / days;
}

export function trendText(perDay: number | null): string {
  if (perDay == null) return "";
  if (Math.abs(perDay) < 1024 * 1024) return "steady";
  return perDay < 0 ? `${bytes(-perDay)} less free a day` : `${bytes(perDay)} more free a day`;
}

/** What the selected items add up to. */
export function selection(items: PreviewItem[], keys: ReadonlySet<string>): { count: number; files: number; bytes: number } {
  const picked = items.filter((i) => keys.has(i.key));
  return { count: picked.length, files: picked.reduce((n, i) => n + i.files, 0), bytes: picked.reduce((n, i) => n + i.bytes, 0) };
}

/** The confirm button is live only with something selected, the box ticked, and no cleanup running. */
export function canConfirm(s: { selected: number; acknowledged: boolean; running: boolean; sending: boolean }): boolean {
  return s.selected > 0 && s.acknowledged && !s.running && !s.sending;
}

export function confirmLabel(action: Action, sel: { count: number; bytes: number }): string {
  return `${ACTIONS[action].verb}: ${sel.count} ${sel.count === 1 ? "item" : "items"}, ${bytes(sel.bytes)}`;
}

/** One line about the latest cleanup job. */
export function jobText(job: CleanupJob): string {
  const what = ACTIONS[job.action]?.title ?? job.action;
  switch (job.status) {
    case "queued":
      return job.error ? `${what}: ${job.error}` : `${what}: waiting to start.`;
    case "leased":
      return `${what}: ${job.done} of ${job.total} done, ${bytes(job.freed_bytes)} freed so far.`;
    case "succeeded": {
      const removed = job.done - job.skipped;
      const skipped = job.skipped ? `, ${job.skipped} skipped (changed since the preview)` : "";
      const errors = job.errors.length ? `, ${job.errors.length} could not be removed` : "";
      return `${what}: ${removed} removed${skipped}${errors}; ${bytes(job.freed_bytes)} freed.`;
    }
    case "failed":
      return `${what} failed: ${job.error ?? "unknown error"}`;
    case "cancelled":
      return `${what} was cancelled after ${job.done} of ${job.total}; ${bytes(job.freed_bytes)} freed.`;
  }
}

export function jobActive(job: CleanupJob | null): boolean {
  return !!job && (job.status === "queued" || job.status === "leased");
}

/** "2026-01-01T02:03:04Z" from "pre-restore-20260101T020304Z". */
export function preRestoreDate(name: string): string | null {
  const m = /^pre-restore-(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(name);
  return m ? `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}Z` : null;
}
