/** Resource limits of the server's own heavy jobs (GET /api/admin/resource-limits,
 * docs/resource-limits.md). Shared by Admin and the job queue. */

export type Blocked = "off" | "feature_limit" | "global_limit" | null;

export interface FeatureLimit {
  feature: string;
  label: string;
  concurrency: number;
  threads: number | null;
  memory_mb: number | null;
  nice: number;
  active: number;
  queued: number;
  running: { job_id: number | null; started_at: string }[];
  blocked: Blocked;
  waiting_for_limit: boolean;
  env: Record<string, string | null>;
}

export interface ResourceLimits {
  heavy_jobs: { limit: number; active: number; env: string };
  features: FeatureLimit[];
}

/** One line for a feature's current state, e.g. "1 running · 2 waiting: limit reached". */
export function limitState(f: FeatureLimit): string {
  if (f.blocked === "off") return f.queued ? `${f.queued} waiting for a worker` : "Workers only";
  const parts: string[] = [];
  if (f.active) parts.push(`${f.active} running`);
  if (f.queued) {
    const why = f.blocked === "feature_limit" ? "limit reached" : f.blocked === "global_limit" ? "heavy-job limit reached" : "";
    parts.push(`${f.queued} waiting${why ? `: ${why}` : ""}`);
  }
  return parts.length ? parts.join(" · ") : "Idle";
}

/** The caps as one short phrase, e.g. "1 at a time · 2 cores · 3 GB · nice 10". */
export function limitCaps(f: FeatureLimit): string {
  if (f.concurrency <= 0) return "Not on this server";
  const parts = [`${f.concurrency} at a time`];
  if (f.threads !== null) parts.push(f.threads ? `${f.threads} ${f.threads === 1 ? "core" : "cores"}` : "all cores");
  parts.push(f.memory_mb ? (f.memory_mb % 1024 === 0 ? `${f.memory_mb / 1024} GB` : `${f.memory_mb} MB`) : "no memory cap");
  parts.push(`nice ${f.nice}`);
  return parts.join(" · ");
}
