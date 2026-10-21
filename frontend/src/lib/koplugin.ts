// KOReader plugin (docs/koreader-plugin.md): plain-language setup state and code countdown.
import { ApiError, csrf, errorText } from "./api";
import type { PluginSetup } from "./types";

/** What the device card says about the plugin's one-time setup link. */
export function pluginSetupText(p: PluginSetup | null | undefined, now = Date.now()): { tone: "ok" | "waiting" | "off"; text: string } | null {
  if (!p) return null;
  switch (p.setup_state) {
    case "used":
      return { tone: "ok", text: "Plugin set up" };
    case "revoked":
      return { tone: "off", text: "Setup link revoked" };
    case "expired":
      return { tone: "off", text: "Setup link expired" };
    case "pending": {
      const left = p.expires_at ? new Date(p.expires_at).getTime() - now : 0;
      if (left <= 0) return { tone: "off", text: "Setup link expired" };
      const days = left >= 86_400_000 ? Math.ceil(left / 86_400_000) : 0;
      const hours = Math.floor(left / 3_600_000);
      return { tone: "waiting", text: `Plugin waiting for first start · link valid ${days >= 1 ? `${days} more day${days === 1 ? "" : "s"}` : hours >= 1 ? `${hours} more hour${hours === 1 ? "" : "s"}` : "under an hour"}` };
    }
  }
}

/** "4:59" until a pairing code expires; "0:00" once it has. */
export function countdown(expiresAt: string, now = Date.now()): string {
  const s = Math.max(0, Math.ceil((new Date(expiresAt).getTime() - now) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** POST for the zip and save it; the server mints a new one-time secret on every call. */
export async function downloadPlugin(deviceId: string, serverUrl: string | null): Promise<void> {
  const res = await fetch(`/api/devices/${deviceId}/koplugin`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json", "x-csrf-token": csrf() },
    body: JSON.stringify(serverUrl ? { server_url: serverUrl } : {}),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    const code = typeof data?.detail === "string" ? data.detail : null;
    throw new ApiError(res.status, code ? errorText(code) : `Request failed (${res.status})`, code);
  }
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = "lectern.koplugin.zip";
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/** True for plain-http addresses outside Tailscale's 100.64.0.0/10: credentials would travel
 * unencrypted. (Inside the tailnet WireGuard encrypts them.) */
export function insecureServerUrl(url: string): boolean {
  let u: URL;
  try {
    u = new URL(url.trim());
  } catch {
    return false;
  }
  if (u.protocol !== "http:") return false;
  const m = /^(\d+)\.(\d+)\.\d+\.\d+$/.exec(u.hostname);
  if (m && Number(m[1]) === 100 && Number(m[2]) >= 64 && Number(m[2]) <= 127) return false;
  return !["localhost", "127.0.0.1", "[::1]"].includes(u.hostname);
}
