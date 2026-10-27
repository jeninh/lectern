// Text positions in a spine item, exactly as the server counts them
// (lectern.positions.normalize_spine_text): text of <body> only, <script>/<style> dropped,
// every whitespace run collapsed to one space, trimmed, offsets in code points.
// The web reader turns its visible range into (spine_index, char) with offsetOf(), and a
// position from another device back into a DOM point with pointAt().

// Python's str.isspace() set, so "whitespace" means the same thing on both sides.
const SPACE = /[\t\n\v\f\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/;
const SKIP = new Set(["script", "style"]);

function bodyOf(doc: Document): Node {
  return doc.body ?? doc.getElementsByTagName("body")[0] ?? doc.documentElement;
}

function* textNodes(root: Node): Generator<Text> {
  const walk = (node: Node): Text[] => {
    const out: Text[] = [];
    for (let c = node.firstChild; c; c = c.nextSibling) {
      if (c.nodeType === 3 || c.nodeType === 4) out.push(c as Text);
      else if (c.nodeType === 1 && !SKIP.has((c as Element).localName.toLowerCase())) out.push(...walk(c));
    }
    return out;
  };
  yield* walk(root);
}

/** Streaming whitespace collapse: how many normalised characters precede each raw point. */
class Counter {
  count = 0;
  pending = false;
  push(ch: string) {
    if (SPACE.test(ch)) {
      if (this.count > 0) this.pending = true;
    } else {
      if (this.pending) this.count += 1;
      this.pending = false;
      this.count += 1;
    }
  }
  /** Position of the next non-space character. */
  get at() {
    return this.count + (this.pending ? 1 : 0);
  }
}

/** The normalised text of a spine item document (for tests and debugging). */
export function normalizedText(doc: Document): string {
  let raw = "";
  for (const t of textNodes(bodyOf(doc))) raw += t.data;
  return raw.split(SPACE).filter(Boolean).join(" ");
}

/** Normalised character offset of the DOM point (node, offset). */
export function offsetOf(doc: Document, node: Node, offset: number): number {
  const point = doc.createRange();
  point.setStart(node, offset);
  point.collapse(true);
  const c = new Counter();
  for (const t of textNodes(bodyOf(doc))) {
    if (t === node) {
      let units = 0;
      for (const ch of t.data) {
        if (units >= offset) break;
        c.push(ch);
        units += ch.length;
      }
      return c.at;
    }
    if (point.comparePoint(t, 0) >= 0) return c.at; // this text starts at or after the point
    for (const ch of t.data) c.push(ch);
  }
  return c.count;
}

/** DOM point at normalised offset ``char`` (clamped to the end of the text). */
export function pointAt(doc: Document, char: number): { node: Text; offset: number } | null {
  const c = new Counter();
  let last: { node: Text; offset: number } | null = null;
  for (const t of textNodes(bodyOf(doc))) {
    let units = 0;
    for (const ch of t.data) {
      if (!SPACE.test(ch) && c.at >= char) return { node: t, offset: units };
      c.push(ch);
      units += ch.length;
    }
    last = { node: t, offset: t.data.length };
  }
  return last;
}

export function rangeAt(doc: Document, char: number): Range | null {
  const p = pointAt(doc, char);
  if (!p) return null;
  const r = doc.createRange();
  r.setStart(p.node, p.offset);
  r.collapse(true);
  return r;
}

/** The DOM range covering normalised offsets [start, end), e.g. a search match. */
export function rangeBetween(doc: Document, start: number, end: number): Range | null {
  const a = pointAt(doc, start);
  const b = pointAt(doc, Math.max(start, end - 1)); // the last character, then past it
  if (!a || !b || b.offset >= b.node.data.length) return null;
  const r = doc.createRange();
  r.setStart(a.node, a.offset);
  r.setEnd(b.node, b.offset + (b.node.data.codePointAt(b.offset)! > 0xffff ? 2 : 1));
  return r;
}
