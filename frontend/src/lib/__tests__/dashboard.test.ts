// @vitest-environment jsdom
// The Devices dashboard (issue #86): the KOReader plugin's report on a device, and one device's
// books, hidden ones without their title.
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { asOfText, batteryLow, batteryText, freeSpaceText, staleSync, versionText } from "../dashboard";
import type { DashboardDevice, DeviceDetail, DeviceReport } from "../types";

const NOW = Date.parse("2026-10-28T12:00:00Z");
vi.mock("../store", () => ({ useStore: () => ({ now: NOW }) }));
const { DeviceDashboardView } = await import("../../components/DeviceDashboard");

const REPORT: DeviceReport = {
  reported_at: "2026-10-28T11:55:00Z",
  as_of: "2026-10-28T11:50:00Z",
  battery: 12,
  charging: false,
  free_bytes: 3 * 1024 ** 3,
  plugin_version: "1.0",
  koreader_version: "v2025.10",
  update_available: true,
  books: 2,
};
const DEVICE: DashboardDevice = {
  id: 7,
  device_id: "k3",
  label: "Boox",
  own: false,
  user: { id: 3, username: "rex", display_name: "Rex" },
  last_sync_at: "2026-10-28T11:00:00Z",
  catalogue_at: null,
  pending: 1,
  report: REPORT,
};

describe("report helpers", () => {
  it("say battery, charging, and low battery only when not charging", () => {
    expect(batteryText(REPORT)).toBe("12%");
    expect(batteryText({ ...REPORT, charging: true })).toBe("12% · charging");
    expect(batteryText({ ...REPORT, battery: null })).toBeNull();
    expect(batteryLow(REPORT)).toBe(true);
    expect(batteryLow({ ...REPORT, charging: true })).toBe(false);
    expect(batteryLow({ ...REPORT, battery: 16 })).toBe(false);
    expect(batteryLow(null)).toBe(false);
  });
  it("say free space, versions, and how old the values are", () => {
    expect(freeSpaceText(REPORT)).toBe("3.00 GB free");
    expect(versionText(REPORT)).toBe("Plugin 1.0 · KOReader v2025.10");
    expect(versionText({ ...REPORT, plugin_version: null })).toBe("KOReader v2025.10");
    expect(versionText({ ...REPORT, plugin_version: null, koreader_version: null })).toBeNull();
    expect(asOfText(REPORT, NOW)).toBe("as of 10 min ago");
  });
  it("call a device stale only past the threshold, by its newest sync of either kind", () => {
    const day = 86_400_000;
    const at = (ms: number) => new Date(NOW - ms).toISOString();
    expect(staleSync({ last_sync_at: at(15 * day), catalogue_at: null }, NOW)).toBe(true);
    expect(staleSync({ last_sync_at: at(15 * day), catalogue_at: at(day) }, NOW)).toBe(false);
    expect(staleSync({ last_sync_at: at(13 * day), catalogue_at: null }, NOW)).toBe(false);
    expect(staleSync({ last_sync_at: null, catalogue_at: null }, NOW)).toBe(false);
  });
});

describe("one device's dashboard", () => {
  beforeAll(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  });
  let root: ReturnType<typeof createRoot> | null = null;
  afterEach(() => {
    act(() => root?.unmount());
    document.body.replaceChildren();
    vi.unstubAllGlobals();
  });

  it("lists the books on it with progress, hides unshared ones, and shows what is still queued", async () => {
    const detail: DeviceDetail = {
      device: DEVICE,
      books: [
        { work: { id: 1, title: "Alpha", author: "Author Alpha", cover_url: null, cover_tint: null }, percentage: 0.5 },
        { work: null, hidden: true, percentage: 0.2 },
        { work: { id: 2, title: "Bravo", author: "Author Bravo", cover_url: null, cover_tint: null }, percentage: null },
      ],
      not_in_library: 1,
      queued: [{ item_id: 9, queued_at: "2026-10-28T10:00:00Z", work: { id: 4, title: "Delta", author: "D", cover_url: null, cover_tint: null } }],
    };
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (u: string) => {
        calls.push(u);
        return new Response(JSON.stringify(detail), { status: 200, headers: { "content-type": "application/json" } });
      }),
    );
    const el = document.createElement("div");
    document.body.append(el);
    root = createRoot(el);
    await act(async () => root!.render(createElement(DeviceDashboardView, { deviceId: 7, latest: "1.1" })));
    await act(async () => new Promise((r) => setTimeout(r, 10)));

    expect(calls).toEqual(["/api/devices/dashboard/7"]);
    const on = document.querySelector('[aria-labelledby="dash-on-h"]')!;
    const rows = [...on.querySelectorAll(".queue-item")].map((li) => li.textContent);
    expect(rows[0]).toContain("Alpha");
    expect(rows[0]).toContain("50%");
    expect(rows[1]).toContain("Bravo");
    expect(rows[1]).toContain("not opened");
    expect(rows[2]).toContain("1 hidden book");
    expect(rows[2]).toContain("No longer shared with Rex");
    expect(on.textContent).toContain("1 other book on the device is no longer in the library");
    const queued = document.querySelector('[aria-labelledby="dash-queued-h"]')!;
    expect(queued.textContent).toContain("Delta");
    expect(document.body.textContent).toContain("update available (1.1)");
    expect(document.querySelector(".device-report dd[data-low]")?.textContent).toBe("12%");
  });

  it("says what a device without a report can show", async () => {
    const detail: DeviceDetail = { device: { ...DEVICE, device_id: "p5", report: null }, books: [], not_in_library: 0, queued: [] };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(detail), { status: 200, headers: { "content-type": "application/json" } })));
    const el = document.createElement("div");
    document.body.append(el);
    root = createRoot(el);
    await act(async () => root!.render(createElement(DeviceDashboardView, { deviceId: 5 })));
    await act(async () => new Promise((r) => setTimeout(r, 10)));
    expect(document.body.textContent).toContain("A catalogue-only device");
    expect(document.querySelector(".device-report")).toBeNull();
  });
});
