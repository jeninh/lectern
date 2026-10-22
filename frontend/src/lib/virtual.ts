// Windowed rendering for long collections that scroll with the page: only the rows near the
// viewport are in the DOM; padding stands in for the rest. Rows have one height (the tallest
// measured since the last resize, since covers and two-line titles vary), so positions are
// arithmetic.

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

/** Columns of a `repeat(auto-fill, minmax(minWidth, 1fr))` grid of the element's width. */
export function useGridColumns(el: HTMLElement | null, minWidth: number, gap: number, enabled = true): { columns: number; width: number } {
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    if (!el) return;
    setWidth(el.clientWidth);
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, [el]);
  const columns = enabled && width ? Math.max(1, Math.floor((width - padX(el) + gap) / (minWidth + gap))) : 1;
  return { columns, width };
}

function padX(el: HTMLElement | null): number {
  if (!el) return 0;
  const cs = getComputedStyle(el);
  return parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
}

export interface WindowRows {
  start: number;
  end: number;
  rows: number;
  /** Row height including the gap below it. */
  stride: number;
  padTop: number;
  padBottom: number;
  /** Scroll so the item at `index` is rendered and visible. */
  reveal: (index: number) => void;
}

/** Space the sticky top bar and bottom selection bar take; kept clear when revealing. */
const TOP_CLEAR = 72;
const BOTTOM_CLEAR = 120;

export function useWindowRows(el: HTMLElement | null, o: { count: number; columns: number; gap: number; estimate: number; overscan?: number; width: number }): WindowRows {
  const overscan = o.overscan ?? 3;
  const rows = Math.ceil(o.count / Math.max(1, o.columns));
  const [rowH, setRowH] = useState(o.estimate);
  const [range, setRange] = useState({ start: 0, end: Math.min(rows, 8) });
  const stride = rowH + o.gap;
  const live = useRef({ rows, stride, overscan });
  live.current = { rows, stride, overscan };

  // A different width or layout changes cover heights: measure again.
  const tallestSeen = useRef(0);
  useLayoutEffect(() => {
    tallestSeen.current = 0;
    setRowH(o.estimate);
  }, [o.width, o.columns, o.estimate]);

  const compute = useCallback(() => {
    if (!el) return;
    const { rows, stride, overscan } = live.current;
    const top = el.getBoundingClientRect().top + window.scrollY;
    const from = window.scrollY - top;
    const start = Math.max(0, Math.min(rows, Math.floor(from / stride) - overscan));
    const end = Math.max(start, Math.min(rows, Math.ceil((from + window.innerHeight) / stride) + overscan));
    setRange((r) => (r.start === start && r.end === end ? r : { start, end }));
  }, [el]);

  useLayoutEffect(compute, [compute, rows, stride]);
  useEffect(() => {
    let frame = 0;
    const on = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(compute);
    };
    window.addEventListener("scroll", on, { passive: true });
    window.addEventListener("resize", on);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("scroll", on);
      window.removeEventListener("resize", on);
    };
  }, [compute]);

  // Rows are as tall as the tallest item measured so far.
  useLayoutEffect(() => {
    if (!el) return;
    let tallest = tallestSeen.current;
    el.querySelectorAll<HTMLElement>("[data-vrow] > *").forEach((c) => {
      tallest = Math.max(tallest, c.offsetHeight);
    });
    tallestSeen.current = tallest;
    if (tallest && Math.abs(Math.ceil(tallest) - rowH) > 0.5) setRowH(Math.ceil(tallest));
  });

  const reveal = useCallback(
    (index: number) => {
      if (!el) return;
      const { stride } = live.current;
      const row = Math.floor(index / Math.max(1, o.columns));
      const top = el.getBoundingClientRect().top + window.scrollY + row * stride;
      const bottom = top + stride;
      if (top < window.scrollY + TOP_CLEAR) window.scrollTo({ top: Math.max(0, top - TOP_CLEAR), behavior: "instant" });
      else if (bottom > window.scrollY + window.innerHeight - BOTTOM_CLEAR) window.scrollTo({ top: bottom - window.innerHeight + BOTTOM_CLEAR, behavior: "instant" });
      compute();
    },
    [el, o.columns, compute],
  );

  const start = Math.min(range.start, rows);
  const end = Math.min(Math.max(range.end, start), rows);
  return { start, end, rows, stride, padTop: start * stride, padBottom: Math.max(0, rows - end) * stride, reveal };
}
