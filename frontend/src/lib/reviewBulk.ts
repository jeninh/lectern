// Which uploads "File N" in Review before filing may file without a person looking at each one.
import { validate } from "./metadata";
import type { ImportInfo } from "./types";

export type Choice = { action: "new" } | { action: "join"; work_id: number };

/** Where an upload goes unless the person picks otherwise: its saved target, else the proposal. */
export function proposedTarget(imp: ImportInfo): Choice {
  const proposal = imp.proposal;
  return imp.target?.action === "join" && imp.target.work_id ? { action: "join", work_id: imp.target.work_id } : proposal?.action === "join" && proposal.work_id ? { action: "join", work_id: proposal.work_id } : { action: "new" };
}

export type BulkPlan = { target: Choice } | { skip: "unsaved" | "uncertain" | "details" };

/**
 * Bulk filing files only what the card shows and nothing a person would want to decide:
 * - a card with unsaved choices (another target, the cover, edited details) is skipped, so a
 *   radio the person picked is never silently replaced by the proposal;
 * - joining an existing book (files are hard to take back out) needs a saved target or a
 *   certain match (`high`, the same bar as auto-file); a likely or possible match is skipped;
 * - details that do not validate are skipped.
 */
export function bulkPlan(imp: ImportInfo, unsaved: boolean): BulkPlan {
  if (unsaved) return { skip: "unsaved" };
  if (!imp.metadata || Object.keys(validate(imp.metadata)).length) return { skip: "details" };
  const savedJoin = imp.target?.action === "join" && !!imp.target.work_id;
  const target = proposedTarget(imp);
  if (target.action === "join" && !savedJoin && imp.proposal?.confidence !== "high") return { skip: "uncertain" };
  return { target };
}
