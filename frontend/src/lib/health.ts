// Health page (docs/health.md): types and the small decisions the page makes.

export type HealthLevel = "ok" | "info" | "warning" | "critical";
export type HealthGroup = "backups" | "disk" | "jobs" | "workers" | "other";

export interface HealthJob {
  id: number;
  kind: string;
  label: string;
  work_id: number | null;
  work_title: string | null;
  error: string | null;
  at: string | null;
}

export interface HealthCheck {
  id: string;
  group: HealthGroup | string;
  title: string;
  level: HealthLevel;
  summary: string;
  detail: Record<string, unknown> & { items?: HealthJob[]; total?: number };
}

export interface HealthWorker {
  id: number;
  label: string;
  state: string;
  seen_at: string | null;
  capabilities: string[];
  unavailable: Record<string, string>;
  version: string | null;
}

export interface HealthView {
  level: HealthLevel;
  checked_at: string;
  checks: HealthCheck[];
  since: Record<string, string | null>;
  workers: HealthWorker[];
  offline: boolean;
}

export interface PushInfo {
  state: "off" | "not_configured" | "offline" | "on";
  kind: "ntfy" | "webhook" | null;
  server: string | null;
  token_set: boolean;
  problems: string[];
  last: { at?: string; ok?: boolean; error?: string | null };
}

export const GROUPS: { id: HealthGroup; title: string }[] = [
  { id: "backups", title: "Backups" },
  { id: "disk", title: "Disk space" },
  { id: "jobs", title: "Jobs" },
  { id: "workers", title: "Workers" },
  { id: "other", title: "Other" },
];

const RANK: Record<string, number> = { ok: 0, info: 0, warning: 1, critical: 2 };

/** The worst level of a set of checks ("info" counts as fine). */
export function worstLevel(levels: string[]): HealthLevel {
  let out: HealthLevel = "ok";
  for (const l of levels) if ((RANK[l] ?? 0) > RANK[out]) out = l as HealthLevel;
  return out;
}

/** Checks by group, in page order, problems first within a group; unknown groups go to Other. */
export function groupChecks(checks: HealthCheck[]): { id: HealthGroup; title: string; level: HealthLevel; checks: HealthCheck[] }[] {
  const known = new Set(GROUPS.map((g) => g.id as string));
  return GROUPS.map((g) => {
    const mine = checks
      .filter((c) => (known.has(c.group) ? c.group : "other") === g.id)
      .sort((a, b) => (RANK[b.level] ?? 0) - (RANK[a.level] ?? 0));
    return { ...g, level: worstLevel(mine.map((c) => c.level)), checks: mine };
  }).filter((g) => g.checks.length > 0);
}

export const LEVEL_LABEL: Record<HealthLevel, string> = { ok: "OK", info: "OK", warning: "Warning", critical: "Critical" };
export const LEVEL_BADGE: Record<HealthLevel, string> = { ok: "ok", info: "", warning: "warn", critical: "danger" };

/** The request that retries a failed job, where the app has one. */
export function retryRequest(job: HealthJob): { path: string; body?: unknown } | null {
  if (!job.work_id) return null;
  switch (job.kind) {
    case "tts":
      return { path: `/api/works/${job.work_id}/audiobook/retry` };
    case "pdf2epub":
      return { path: `/api/works/${job.work_id}/pdf-epub/retry` };
    case "align":
      return { path: `/api/works/${job.work_id}/align`, body: { force: true } };
    default:
      return null;
  }
}

/** One sentence on the push channel; never shows more than the masked server. */
export function pushLine(p: PushInfo): string {
  switch (p.state) {
    case "off":
      return "Off. Set LECTERN_ALERT_NTFY_URL (or LECTERN_ALERT_WEBHOOK_URL) in .env to also get alerts on your phone.";
    case "not_configured":
      return `Not set up correctly: check ${p.problems.join(", ")} in .env.`;
    case "offline":
      return "Paused while offline mode is on: no connection is attempted and alerts stay in the app.";
    default:
      return `On: ${p.kind === "ntfy" ? "ntfy" : "webhook"} at ${p.server}${p.token_set ? " (with a token)" : ""}.`;
  }
}
