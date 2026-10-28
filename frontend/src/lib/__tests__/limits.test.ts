import { describe, expect, it } from "vitest";
import { limitCaps, limitState, type FeatureLimit } from "../limits";

const f = (over: Partial<FeatureLimit> = {}): FeatureLimit => ({
  feature: "kokoro",
  label: "Kokoro narration",
  concurrency: 1,
  threads: 2,
  memory_mb: 3072,
  nice: 10,
  active: 0,
  queued: 0,
  running: [],
  blocked: null,
  waiting_for_limit: false,
  env: {},
  ...over,
});

describe("resource limits", () => {
  it("says why queued jobs wait", () => {
    expect(limitState(f())).toBe("Idle");
    expect(limitState(f({ active: 1, queued: 2, blocked: "feature_limit" }))).toBe("1 running · 2 waiting: limit reached");
    expect(limitState(f({ queued: 1, blocked: "global_limit" }))).toBe("1 waiting: heavy-job limit reached");
    expect(limitState(f({ concurrency: 0, queued: 3, blocked: "off" }))).toBe("3 waiting for a worker");
    // queued but free to start: about to be picked up, not waiting for a limit
    expect(limitState(f({ queued: 1 }))).toBe("1 waiting");
  });

  it("describes the caps", () => {
    expect(limitCaps(f())).toBe("1 at a time · 2 cores · 3 GB · nice 10");
    expect(limitCaps(f({ threads: 0, memory_mb: 1536 }))).toBe("1 at a time · all cores · 1536 MB · nice 10");
    expect(limitCaps(f({ threads: null, memory_mb: null, nice: 19 }))).toBe("1 at a time · no memory cap · nice 19");
    expect(limitCaps(f({ concurrency: 0 }))).toBe("Not on this server");
  });
});
