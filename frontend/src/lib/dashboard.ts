// Pure helpers for the Devices dashboard (issue #86), kept out of the components so they can be tested.
import { bytes, relativeTime } from "./format";
import type { DashboardDevice, DeviceReport } from "./types";

/** "41% · charging", or null when the plugin has not reported a battery level. */
export function batteryText(r: DeviceReport | null | undefined): string | null {
  if (!r || r.battery == null) return null;
  return `${r.battery}%${r.charging ? " · charging" : ""}`;
}

/** Low battery (and not charging): a hint for the card, and later for alerts (#75). */
export function batteryLow(r: DeviceReport | null | undefined, threshold = 15): boolean {
  return !!r && r.battery != null && r.battery <= threshold && !r.charging;
}

export function freeSpaceText(r: DeviceReport | null | undefined): string | null {
  if (!r || r.free_bytes == null) return null;
  return `${bytes(r.free_bytes)} free`;
}

/** "Plugin 1.0 · KOReader v2025.10", whichever of the two is known. */
export function versionText(r: DeviceReport | null | undefined): string | null {
  if (!r) return null;
  const parts = [r.plugin_version && `Plugin ${r.plugin_version}`, r.koreader_version && `KOReader ${r.koreader_version}`].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

/** "as of 10 min ago" for the values in a report. */
export function asOfText(r: DeviceReport | null | undefined, now: number): string | null {
  return r?.as_of ? `as of ${relativeTime(r.as_of, now)}` : null;
}

/** Not synced (progress or catalogue) in ``days`` days: shown on the card, and later fed to #75's alerts. */
export function staleSync(d: Pick<DashboardDevice, "last_sync_at" | "catalogue_at">, now: number, days = 14): boolean {
  const times = [d.last_sync_at, d.catalogue_at].filter((t): t is string => !!t).map((t) => new Date(t).getTime());
  if (!times.length) return false;
  return now - Math.max(...times) > days * 86_400_000;
}
