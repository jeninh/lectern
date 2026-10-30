// Cover editor maths and request rules (issue #85): the fixed 2:3 crop, minimum size, preview
// sizes, the debounced candidate request, radiogroup keys, and what each pick or revert saves.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CANDIDATE_DELAY_MS,
  PREVIEW_SIZES,
  candidateDraft,
  candidatesUrl,
  clampBox,
  createCandidateLoader,
  cropTooSmall,
  imageTooSmall,
  initialBox,
  isWholeImage,
  moveBox,
  normaliseCrop,
  previewLayout,
  radioKeyTarget,
  resizeBox,
  revertDraft,
  selectedIndex,
} from "../covers";
import type { CoverCandidate, CoverHistoryEntry } from "../types";

describe("crop box", () => {
  it("starts as the largest centred 2:3 box (landscape and portrait sources)", () => {
    // 1000×500: height limits the box to 333.33×500, centred horizontally.
    const l = initialBox(1000, 500);
    expect(l.h).toBe(500);
    expect(l.w).toBeCloseTo(333.333, 2);
    expect(l.x).toBeCloseTo(333.333, 2);
    expect(l.y).toBe(0);
    // 600×1200: width limits it to 600×900, centred vertically.
    expect(initialBox(600, 1200)).toEqual({ x: 0, y: 150, w: 600, h: 900 });
  });

  it("keeps 2:3 and stays inside the image when moved or oversized", () => {
    const b = clampBox({ x: -50, y: 9999, w: 5000, h: 1 }, 600, 1200);
    expect(b).toEqual({ x: 0, y: 300, w: 600, h: 900 });
    expect(moveBox({ x: 100, y: 100, w: 300, h: 450 }, 10_000, -10_000, 600, 1200)).toEqual({ x: 300, y: 0, w: 300, h: 450 });
    for (const [iw, ih] of [[800, 800], [1000, 500], [333, 777]]) {
      const m = moveBox(initialBox(iw, ih), 41, -17, iw, ih);
      expect(m.w / m.h).toBeCloseTo(2 / 3, 10);
      expect(m.x).toBeGreaterThanOrEqual(0);
      expect(m.x + m.w).toBeLessThanOrEqual(iw + 1e-9);
      expect(m.y + m.h).toBeLessThanOrEqual(ih + 1e-9);
    }
  });

  it("zooms around the centre, never below 200 px or above the image", () => {
    const b = { x: 100, y: 200, w: 400, h: 600 };
    const small = resizeBox(b, 300, 800, 1200);
    expect(small).toEqual({ x: 150, y: 275, w: 300, h: 450 });
    expect(resizeBox(b, 10, 800, 1200).w).toBe(200);
    expect(resizeBox(b, 9999, 800, 1200)).toEqual({ x: 0, y: 0, w: 800, h: 1200 });
    // An image under 200 px wide: the only box is the whole width.
    expect(resizeBox({ x: 0, y: 0, w: 150, h: 225 }, 100, 150, 400).w).toBe(150);
  });

  it("normalises to fractions inside 0–1", () => {
    expect(normaliseCrop({ x: 100, y: 150, w: 300, h: 450 }, 600, 1200)).toEqual({ x: 0.166667, y: 0.125, w: 0.5, h: 0.375 });
    // A box that overshoots by float noise is pulled back so x + w ≤ 1.
    const n = normaliseCrop({ x: 200.0000004, y: 0, w: 400, h: 600 }, 600, 900);
    expect(n.x + n.w).toBeLessThanOrEqual(1);
    expect(isWholeImage(normaliseCrop({ x: 0, y: 0, w: 600, h: 900 }, 600, 900))).toBe(true);
    expect(isWholeImage(normaliseCrop({ x: 0, y: 0, w: 300, h: 450 }, 600, 900))).toBe(false);
  });

  it("flags a crop under 200×300 source pixels", () => {
    expect(cropTooSmall({ x: 0, y: 0, w: 0.5, h: 0.5 }, 400, 600)).toBe(false); // 200×300 exactly
    expect(cropTooSmall({ x: 0, y: 0, w: 0.49, h: 0.5 }, 400, 600)).toBe(true); // 196 wide
    expect(cropTooSmall({ x: 0, y: 0, w: 1, h: 1 }, 150, 225)).toBe(true);
    expect(imageTooSmall(150, 900)).toBe(true);
    expect(imageTooSmall(200, 300)).toBe(false);
    expect(imageTooSmall(900, 290)).toBe(true); // wide but short: the 2:3 box is 193 wide
  });
});

describe("previews", () => {
  it("has a library-card size and a 100 px thumbnail, both 2:3", () => {
    expect(PREVIEW_SIZES.thumb).toEqual({ w: 100, h: 150 });
    expect(PREVIEW_SIZES.card.w / PREVIEW_SIZES.card.h).toBeCloseTo(2 / 3, 10);
  });

  it("scales and shifts the image so exactly the crop shows", () => {
    // Crop = right-hand half, top-quarter of the image: at 100 px wide the image is drawn 200 px wide.
    const p = previewLayout({ x: 0.5, y: 0, w: 0.5, h: 0.25 }, 100);
    expect(p.height).toBe(150);
    expect(p.backgroundSize).toBe("200px 600px");
    expect(p.backgroundPosition).toBe("-100px 0px");
  });
});

describe("candidate loader", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  const q = (title: string, offset = 0) => ({ workId: 7, title, author: "A", font: "display" as const, offset });
  const reply = (body: unknown) => ({ ok: true, status: 200, json: async () => body }) as Response;

  it("builds the request the API expects", () => {
    expect(candidatesUrl({ ...q("  Dune "), author: "Frank Herbert", font: "noto", offset: 6 })).toBe("/api/works/7/cover/candidates?title=Dune&author=Frank+Herbert&font=noto&offset=6&count=6");
  });

  it("waits 400 ms of quiet, asks once for the last text, and aborts the superseded request", async () => {
    const signals: AbortSignal[] = [];
    const urls: string[] = [];
    const fetcher = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      urls.push(String(url));
      signals.push(init!.signal as AbortSignal);
      return reply({ fonts: ["display"], styles: [], candidates: [{ seed: urls.length }] });
    });
    const results: number[] = [];
    const loader = createCandidateLoader({ fetcher: fetcher as unknown as typeof fetch, onResult: (r) => results.push((r.candidates[0] as unknown as { seed: number }).seed), onError: () => {} });
    loader.schedule(q("D"));
    await vi.advanceTimersByTimeAsync(CANDIDATE_DELAY_MS - 1);
    loader.schedule(q("Du")); // typing resets the wait
    await vi.advanceTimersByTimeAsync(CANDIDATE_DELAY_MS - 1);
    expect(fetcher).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(urls[0]).toContain("title=Du&");
    // A request in flight is aborted by the next one, and its answer is never delivered.
    let finish!: (r: Response) => void;
    fetcher.mockImplementationOnce(async (_u, init) => {
      signals.push(init!.signal as AbortSignal);
      return new Promise<Response>((res) => (finish = res));
    });
    loader.now(q("Dun"));
    await vi.advanceTimersByTimeAsync(0);
    const stale = signals[signals.length - 1];
    loader.schedule(q("Dune"));
    expect(stale.aborted).toBe(true);
    finish(reply({ fonts: [], styles: [], candidates: [{ seed: 99 }] }));
    await vi.advanceTimersByTimeAsync(CANDIDATE_DELAY_MS);
    expect(results).not.toContain(99);
    expect(results).toHaveLength(2);
  });

  it("reports a failed request, but not an aborted one", async () => {
    const errors: string[] = [];
    const fetcher = vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) }) as Response);
    const loader = createCandidateLoader({ fetcher: fetcher as unknown as typeof fetch, onResult: () => {}, onError: (m) => errors.push(m) });
    loader.now(q("x"));
    await vi.advanceTimersByTimeAsync(0);
    expect(errors).toEqual(["Could not load cover designs."]);
    loader.schedule(q("y"));
    loader.cancel();
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});

const cand = (seed: number, over: Partial<CoverCandidate> = {}): CoverCandidate => ({ style: "classic", seed, font: "display", palette: "Ink", url: `/u/${seed}`, ...over });

describe("radiogroup keys and picks", () => {
  it("steps and wraps, jumps with Home and End, ignores other keys", () => {
    expect(radioKeyTarget("ArrowRight", 5, 6)).toBe(0);
    expect(radioKeyTarget("ArrowDown", 1, 6)).toBe(2);
    expect(radioKeyTarget("ArrowLeft", 0, 6)).toBe(5);
    expect(radioKeyTarget("ArrowUp", 3, 6)).toBe(2);
    expect(radioKeyTarget("Home", 4, 6)).toBe(0);
    expect(radioKeyTarget("End", 1, 6)).toBe(5);
    expect(radioKeyTarget("a", 1, 6)).toBeNull();
    expect(radioKeyTarget("ArrowRight", 0, 0)).toBeNull();
  });

  it("picking sets a generate draft with style, seed, and font; selection matches on all three", () => {
    const c = cand(3, { style: "modern", font: "noto" });
    expect(candidateDraft(c)).toEqual({ action: "generate", style: "modern", seed: 3, font: "noto" });
    const list = [cand(3), cand(3, { font: "noto", style: "modern" }), cand(4)];
    expect(selectedIndex(list, candidateDraft(list[1]))).toBe(1);
    expect(selectedIndex(list, { action: "generate", style: "classic", seed: 3 })).toBe(0); // font defaults to display
    expect(selectedIndex(list, { action: "upload", token: "t" })).toBe(-1);
  });
});

describe("history revert", () => {
  const base = { current: false, revision_id: 12, url: "/h/12.jpg" };
  it("reverts an image by its revision and a generated cover by its design", () => {
    expect(revertDraft({ ...base, kind: "image", source: "upload" })).toEqual({ action: "revision", revision_id: 12, preview: "/h/12.jpg" });
    const g: CoverHistoryEntry = { ...base, kind: "generated", style: "minimal", seed: 4, font: "text" };
    expect(revertDraft(g)).toEqual({ action: "generate", style: "minimal", seed: 4, font: "text" });
    expect(revertDraft({ ...g, style: null, seed: undefined, font: undefined })).toEqual({ action: "generate", style: null, seed: 0, font: "display" });
  });
  it("has nothing to revert to for the current cover", () => {
    expect(revertDraft({ ...base, current: true, revision_id: null, kind: "image" })).toBeNull();
  });
});
