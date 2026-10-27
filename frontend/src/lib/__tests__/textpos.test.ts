// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { normalizedText, offsetOf, pointAt, rangeBetween } from "../textpos";

// Spine item 2 of tests/fixtures/magi (Project Gutenberg #7256, public domain) and the
// server's normalised length and hand-checked sentence spans for it (sentence_map.json).
const xhtml = readFileSync(join(__dirname, "fixtures/magi-spine-2.xhtml"), "utf8");
const expected = JSON.parse(readFileSync(join(__dirname, "fixtures/magi-spine-2.json"), "utf8"));
const parse = (s: string) => new DOMParser().parseFromString(s, "application/xhtml+xml");

describe("text positions match lectern.positions.normalize_spine_text", () => {
  const doc = parse(xhtml);
  const text = normalizedText(doc);

  it("normalises to the same text and offsets as the server", () => {
    expect(text.length).toBe(expected.normalized_length);
    for (const s of expected.sentences) expect(text.slice(s.char_start, s.char_end)).toBe(s.text);
  });

  it("maps sentence starts to DOM points and back", () => {
    for (const s of expected.sentences) {
      const p = pointAt(doc, s.char_start)!;
      expect(p.node.data.slice(p.offset).replace(/\s+/g, " ").trimStart().startsWith(s.text.slice(0, 12))).toBe(true);
      expect(offsetOf(doc, p.node, p.offset)).toBe(s.char_start);
    }
  });

  it("round-trips every offset", () => {
    for (let c = 0; c < text.length; c += 97) {
      const p = pointAt(doc, c)!;
      expect(offsetOf(doc, p.node, p.offset)).toBe(text[c] === " " ? c + 1 : c);
    }
  });

  it("skips script and style, collapses whitespace, counts code points", () => {
    const d = parse(`<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body>
      <style>p{}</style><p>  Hello,\u00a0\n world</p><script>var x</script><p>😀 Two</p></body></html>`);
    // Same as the server: no whitespace between the paragraphs' text, so none is added.
    expect(normalizedText(d)).toBe("Hello, world😀 Two");
    const two = d.getElementsByTagName("p")[1].firstChild!;
    expect(offsetOf(d, two, 3)).toBe(14); // UTF-16 offset 3 is after "😀 "; the emoji is one char
    expect(offsetOf(d, d.getElementsByTagName("p")[1], 0)).toBe(12);
    expect(pointAt(d, 99)!.node.data).toBe("😀 Two"); // clamped to the end
  });
});

describe("rangeBetween (search matches)", () => {
  it("covers exactly the normalised span, across elements and collapsed whitespace", () => {
    // normalised: "LoomingsCall me Ishmael. 😀 Some years ago" (no space between h1 and p)
    const d = parse(`<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Loomings</h1><p>Call  me <em>Ish</em>mael.\n 😀 Some years ago</p></body></html>`);
    const text = normalizedText(d);
    const at = (s: string) => text.indexOf(s);
    expect(rangeBetween(d, at("Call me Ishmael"), at("Call me Ishmael") + 15)!.toString()).toBe("Call  me Ishmael");
    expect(rangeBetween(d, at("Loomings"), at("Loomings") + 8)!.toString()).toBe("Loomings");
    // a surrogate pair is one character on the timeline and two UTF-16 units in the DOM
    expect(rangeBetween(d, at("😀"), at("😀") + 1)!.toString()).toBe("😀");
    expect(rangeBetween(d, at("😀"), at("😀") + 6)!.toString()).toBe("😀 Some");
    expect(rangeBetween(d, text.length + 5, text.length + 9)).toBeNull();
  });
});
