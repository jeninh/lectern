import { describe, expect, it } from "vitest";
import { aboutDuration, alignmentHint } from "../readalong";
import type { AlignmentJob, AlignmentSummary } from "../types";

const NOW = Date.parse("2026-10-21T12:00:00Z");
const job = (over: Partial<AlignmentJob> = {}): AlignmentJob => ({
  id: 7, status: "queued", attempts: 0, max_attempts: 3, progress: null, message: null,
  heartbeat_at: null, run_after: null, eta_s: null, error: null, ...over,
});
const al = (over: Partial<AlignmentSummary> = {}): AlignmentSummary => ({ status: "pending", model: "small", coverage: null, ...over });

describe("alignmentHint", () => {
  it("shows the heartbeat's progress and the ETA of a running job", () => {
    const h = alignmentHint(al({ status: "running", job: job({ status: "leased", attempts: 1, progress: 0.427, eta_s: 750 }) }), NOW);
    expect(h).toMatchObject({ text: "Aligning text and audio · 42% · about 13 min left", progress: 0.427, retryModel: null });
  });
  it("names the attempt when a running job is a retry, and omits an unknown ETA", () => {
    const h = alignmentHint(al({ status: "running", job: job({ status: "leased", attempts: 2, progress: 0 }) }), NOW);
    expect(h?.text).toBe("Aligning text and audio · 0% · attempt 2 of 3");
  });
  it("says nothing can run a queued job, with the how-to link, instead of 'Aligning…'", () => {
    const unavailable = { reason: "not_installed", message: "No alignment engine.", docs_url: "https://x/docs#turning-it-on" };
    const h = alignmentHint(al({ job: job(), unavailable }), NOW);
    expect(h).toMatchObject({ text: "No alignment engine.", docsUrl: "https://x/docs#turning-it-on", progress: null, retryModel: null });
  });
  it("shows a failed attempt waiting out its backoff with the first line of the error", () => {
    const run_after = new Date(NOW + 125_000).toISOString();
    const h = alignmentHint(al({ job: job({ attempts: 1, error: "lease expired\nmore detail", run_after }) }), NOW);
    expect(h?.text).toBe("Attempt 1 of 3 failed: lease expired · retrying in 2 min");
    expect(h?.detail).toBe("lease expired\nmore detail");
  });
  it("offers Retry with the same model once attempts are exhausted", () => {
    const h = alignmentHint(al({ status: "failed", model: "faster-whisper-small-int8", error: "lease expired", retry_model: "small", job: job({ status: "failed", attempts: 3, error: "lease expired" }) }), NOW);
    expect(h).toMatchObject({ text: "Alignment failed: lease expired", retryModel: "small", retryLabel: "Retry" });
  });
  it("names the smaller model a Retry runs after running out of memory", () => {
    const error = "out of memory: the alignment was killed (SIGKILL)";
    const h = alignmentHint(al({ status: "failed", model: "faster-whisper-small-int8", error, retry_model: "base" }), NOW);
    expect(h).toMatchObject({ retryModel: "base", retryLabel: "Retry with base" });
  });
  it("offers no Retry when the server has no safe model, and shows readers no error text", () => {
    expect(alignmentHint(al({ status: "failed", model: "tiny", error: "out of memory", retry_model: null }), NOW)).toMatchObject({ retryModel: null, retryLabel: null });
    expect(alignmentHint(al({ status: "failed", error: null }), NOW)).toMatchObject({ text: "Alignment failed", retryModel: null });
  });
  it("has nothing to say about a finished alignment", () => {
    expect(alignmentHint(al({ status: "succeeded", job: job({ status: "succeeded" }) }), NOW)).toBeNull();
    expect(alignmentHint(null, NOW)).toBeNull();
  });
});

describe("aboutDuration", () => {
  it("rounds to minutes and hours", () => {
    expect(aboutDuration(20)).toBe("under a minute");
    expect(aboutDuration(89)).toBe("about 1 min");
    expect(aboutDuration(3600)).toBe("about 1 h");
    expect(aboutDuration(7500)).toBe("about 2 h 5 min");
  });
});
