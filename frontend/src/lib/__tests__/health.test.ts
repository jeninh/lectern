import { describe, expect, it } from "vitest";
import { groupChecks, pushLine, retryRequest, worstLevel, type HealthCheck, type HealthJob, type PushInfo } from "../health";

const check = (id: string, group: string, level: HealthCheck["level"]): HealthCheck => ({ id, group, title: id, level, summary: "", detail: {} });
const job = (kind: string, work_id: number | null = 7): HealthJob => ({ id: 1, kind, label: kind, work_id, work_title: null, error: null, at: null });

describe("worstLevel", () => {
  it("treats info as fine and critical as worst", () => {
    expect(worstLevel(["info", "ok"])).toBe("ok");
    expect(worstLevel(["ok", "warning", "info"])).toBe("warning");
    expect(worstLevel(["warning", "critical", "ok"])).toBe("critical");
    expect(worstLevel([])).toBe("ok");
  });
});

describe("groupChecks", () => {
  it("keeps page order, puts problems first, and files unknown groups (restore drill) under Other unless named", () => {
    const g = groupChecks([
      check("disk.data", "disk", "ok"),
      check("disk.backups", "disk", "critical"),
      check("backups.nightly", "backups", "info"),
      check("drill", "drills", "warning"),
    ]);
    expect(g.map((x) => x.id)).toEqual(["backups", "disk", "other"]);
    expect(g[1].checks.map((c) => c.id)).toEqual(["disk.backups", "disk.data"]);
    expect(g[1].level).toBe("critical");
    expect(g[2].checks[0].id).toBe("drill");
  });
});

describe("retryRequest", () => {
  it("uses each kind's own retry and none where the app has none", () => {
    expect(retryRequest(job("tts"))).toEqual({ path: "/api/works/7/audiobook/retry" });
    expect(retryRequest(job("pdf2epub"))?.path).toBe("/api/works/7/pdf-epub/retry");
    expect(retryRequest(job("align"))).toEqual({ path: "/api/works/7/align", body: { force: true } });
    expect(retryRequest(job("ocr"))).toBeNull();
    expect(retryRequest(job("tts", null))).toBeNull();
  });
});

describe("pushLine", () => {
  const base: PushInfo = { state: "on", kind: "ntfy", server: "https://ntfy.example.ts.net/…", token_set: true, problems: [], last: {} };
  it("explains offline mode and names keys to fix", () => {
    expect(pushLine({ ...base, state: "offline" })).toMatch(/offline mode/);
    expect(pushLine({ ...base, state: "not_configured", problems: ["LECTERN_ALERT_NTFY_URL"] })).toContain("LECTERN_ALERT_NTFY_URL");
    expect(pushLine(base)).toBe("On: ntfy at https://ntfy.example.ts.net/… (with a token).");
  });
});
