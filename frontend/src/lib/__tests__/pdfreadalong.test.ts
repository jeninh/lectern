import { describe, expect, it } from "vitest";
import { type Affine, boxesFor, buildTimeline, hit, pageOf, startOf, toPdfPoint, toRect, workSentenceAt } from "../pdfreadalong";
import type { PdfSentenceBoxes, SentenceInfo } from "../types";

// pdf.js PageViewport transforms (derived from its constructor) at scale 2.
const S = 2;
const upright: Affine = [S, 0, 0, -S, 0, 800 * S]; // viewBox [0 0 600 800], /Rotate 0
const offset: Affine = [S, 0, 0, -S, -20 * S, 830 * S]; // viewBox [20 30 620 830]
const turned: Affine = [0, S, S, 0, 0, 0]; // viewBox [0 0 600 800], /Rotate 90

describe("PDF boxes on the rendered page", () => {
  it("flips the y axis: a line near the top of the page is near the top of the canvas", () => {
    expect(toRect(upright, [0, 10, 700, 110, 720], 0)).toEqual({ left: 20, top: 160, width: 200, height: 40 });
  });

  it("honours a media box that does not start at the origin", () => {
    expect(toRect(offset, [0, 20, 810, 70, 830], 0)).toEqual({ left: 0, top: 0, width: 100, height: 40 });
  });

  it("follows /Rotate 90: the page's top edge is on the right", () => {
    expect(toRect(turned, [0, 10, 700, 110, 720], 0)).toEqual({ left: 1400, top: 20, width: 40, height: 200 });
  });

  it("pads the highlight and maps a click back to PDF space", () => {
    expect(toRect(upright, [0, 10, 700, 110, 720], 2)).toEqual({ left: 18, top: 158, width: 204, height: 44 });
    expect(toPdfPoint(upright, 20, 160)).toEqual([10, 720]);
    expect(toPdfPoint(offset, 0, 0)).toEqual([20, 830]);
    expect(toPdfPoint(turned, 1400, 20)).toEqual([10, 700]);
  });
});

const ws = (n: number, start: number | null): SentenceInfo => ({
  n, spine_index: 0, char_start: 0, char_end: 1, audio_start_s: start, audio_end_s: start == null ? null : start + 1, matched: start != null,
});
// Work sentence 0 is EPUB front matter the PDF lacks; work 2 has no audio; work 3 crosses a page.
const work = [ws(0, 0), ws(1, 4), ws(2, null), ws(3, 9)];
const pdf: PdfSentenceBoxes[] = [
  { n: 0, work_n: 1, page: 0, boxes: [[0, 50, 700, 300, 712]] },
  { n: 1, work_n: 2, page: 0, boxes: [[0, 50, 680, 200, 692]] },
  { n: 2, work_n: 3, page: 0, boxes: [[0, 50, 60, 300, 72], [1, 50, 700, 120, 712]] },
  { n: 3, work_n: null, page: 1, boxes: [[1, 50, 600, 300, 612]] },
];
const tl = buildTimeline(work, pdf);

describe("the audio timeline on PDF pages", () => {
  it("finds the sentence playing, skipping sentences without audio", () => {
    expect(workSentenceAt(tl, -1)).toBe(-1);
    expect(workSentenceAt(tl, 3.9)).toBe(0);
    expect(workSentenceAt(tl, 4)).toBe(1);
    expect(workSentenceAt(tl, 8.9)).toBe(1); // 2 has no audio: 1 stays lit until 3 starts
    expect(workSentenceAt(tl, 9)).toBe(3);
  });

  it("highlights every box of a sentence, across pages, and nothing for text the PDF lacks", () => {
    expect(boxesFor(tl, 3)).toEqual([[0, 50, 60, 300, 72], [1, 50, 700, 120, 712]]);
    expect(boxesFor(tl, 0)).toEqual([]);
    expect(pageOf(tl, 3)).toBe(1);
    expect(pageOf(tl, 0)).toBeNull();
  });

  it("click-to-seek hits the box under the pointer, or the nearest within reach", () => {
    expect(hit(tl, 1, 100, 705)).toBe(1);
    expect(hit(tl, 1, 100, 696)).toBe(1); // between lines, 4 points from both: the first wins
    expect(hit(tl, 1, 100, 690)).toBe(2);
    expect(hit(tl, 2, 60, 705)).toBe(3); // the continuation on page 2
    expect(hit(tl, 2, 60, 605)).toBeNull(); // a PDF sentence with no work sentence
    expect(hit(tl, 1, 500, 400)).toBeNull(); // empty margin
  });

  it("seeks to the sentence's audio, or the next sentence that has some", () => {
    expect(startOf(tl, 1)).toBe(4);
    expect(startOf(tl, 2)).toBe(9);
    expect(startOf(tl, 4)).toBeNull();
  });
});
