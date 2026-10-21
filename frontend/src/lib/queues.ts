// Pure helpers for per-device queues, kept out of the components so they can be tested.
import type { QueueDevice, QueueResult } from "./types";

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** One toast line for the outcome of sending books to a device. Failures are named. */
export function summarizeQueueResults(results: QueueResult[], device: Pick<QueueDevice, "label" | "own" | "user">): { text: string; kind: "info" | "error" } {
  const n = (s: QueueResult["status"][]) => results.filter((r) => s.includes(r.status)).length;
  const sent = n(["queued", "requeued"]);
  const already = n(["already_queued"]);
  const hidden = n(["not_visible_to_device"]);
  const noEbook = n(["no_ebook"]);
  const missing = n(["not_found"]);
  const where = device.own ? device.label : `${device.user.display_name || device.user.username}’s ${device.label}`;
  const parts: string[] = [];
  if (sent) parts.push(`Sent ${plural(sent, "book")} to ${where}`);
  if (already) parts.push(sent ? `${already} already waiting` : `${plural(already, "book")} already waiting on ${where}`);
  if (hidden) parts.push(`${plural(hidden, "book")} not shared with ${device.user.display_name || device.user.username}`);
  if (noEbook) parts.push(`${plural(noEbook, "book")} without an EPUB, PDF, or comic`);
  if (missing) parts.push(`${plural(missing, "book")} not found`);
  const failed = hidden + noEbook + missing;
  return { text: parts.join(" · ") || "Nothing to send", kind: failed && !sent && !already ? "error" : "info" };
}

/** ``ids`` with the item at ``index`` moved by ``delta`` places (clamped). */
export function moveItem<T>(ids: T[], index: number, delta: number): T[] {
  const to = Math.max(0, Math.min(ids.length - 1, index + delta));
  if (to === index || index < 0 || index >= ids.length) return ids.slice();
  const out = ids.slice();
  const [item] = out.splice(index, 1);
  out.splice(to, 0, item);
  return out;
}

/** Devices grouped for the picker: the caller's own first, then one group per person. */
export function groupDevices(devices: QueueDevice[]): { label: string; devices: QueueDevice[] }[] {
  const own = devices.filter((d) => d.own);
  const groups: { label: string; devices: QueueDevice[] }[] = own.length ? [{ label: "Your devices", devices: own }] : [];
  const others = new Map<number, QueueDevice[]>();
  for (const d of devices) {
    if (d.own) continue;
    others.set(d.user.id, [...(others.get(d.user.id) ?? []), d]);
  }
  for (const list of others.values()) {
    const u = list[0].user;
    groups.push({ label: u.display_name || u.username, devices: list });
  }
  return groups;
}
