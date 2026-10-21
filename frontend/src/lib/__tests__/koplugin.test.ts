import { describe, expect, it } from "vitest";
import { countdown, pluginSetupText } from "../koplugin";
import type { PluginSetup } from "../types";

const NOW = Date.parse("2026-10-21T12:00:00Z");
const setup = (state: PluginSetup["setup_state"], expiresInMs: number): PluginSetup => ({
  setup_state: state,
  server_url: "http://100.1.2.3:8080",
  downloaded_at: null,
  expires_at: new Date(NOW + expiresInMs).toISOString(),
  set_up_at: null,
});

describe("pluginSetupText", () => {
  it("rounds days up, then counts hours, of a pending link", () => {
    expect(pluginSetupText(setup("pending", 6.5 * 86_400_000), NOW)?.text).toMatch(/7 more days$/);
    expect(pluginSetupText(setup("pending", 86_400_000), NOW)?.text).toMatch(/1 more day$/);
    expect(pluginSetupText(setup("pending", 2.5 * 3_600_000), NOW)?.text).toMatch(/2 more hours$/);
    expect(pluginSetupText(setup("pending", 60_000), NOW)?.text).toMatch(/under an hour$/);
  });
  it("treats a pending link past its expiry as expired", () => {
    expect(pluginSetupText(setup("pending", -1), NOW)).toEqual({ tone: "off", text: "Setup link expired" });
  });
  it("names used and revoked links, and nothing for a device without a zip", () => {
    expect(pluginSetupText(setup("used", 0), NOW)?.tone).toBe("ok");
    expect(pluginSetupText(setup("revoked", 1e9), NOW)?.text).toBe("Setup link revoked");
    expect(pluginSetupText(null, NOW)).toBeNull();
  });
});

describe("countdown", () => {
  it("rounds up to the second and stops at zero", () => {
    expect(countdown(new Date(NOW + 300_000).toISOString(), NOW)).toBe("5:00");
    expect(countdown(new Date(NOW + 61_001).toISOString(), NOW)).toBe("1:02");
    expect(countdown(new Date(NOW - 5_000).toISOString(), NOW)).toBe("0:00");
  });
});
