// Wires the pure selection model (./selection) to a collection in the DOM: roving focus,
// pointer and keyboard selection, touch long-press, an optional marquee, and the window-level
// Cmd/Ctrl+A and Esc. Each item carries `data-sel-key`; consumers spread `cellProps(key)` on it
// and `scopeProps` on the element that contains every item (it needs `position: relative` for
// the marquee).
//
// Never hijacked: keys typed in inputs and other controls inside items, Cmd/Ctrl+K, and pages
// without a collection (the reader). Behind a dialog, only the dialog's own collection listens.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { isEditable, isMac } from "./hooks";
import * as S from "./selection";

export const LONG_PRESS_MS = 500;
const INTERACTIVE = "a[href], button, input, select, textarea, label, summary, [contenteditable='true'], [role=switch], [role=radio], [role=checkbox], [role=menuitem]";

/** Cmd on a Mac, Ctrl elsewhere. */
const modOf = (e: { metaKey: boolean; ctrlKey: boolean }) => (isMac ? e.metaKey : e.ctrlKey);
/** The other one (Ctrl on a Mac, Win elsewhere) belongs to the OS: Ctrl+click is a right click. */
const foreignMod = (e: { metaKey: boolean; ctrlKey: boolean }) => (isMac ? e.ctrlKey : e.metaKey);

export interface SelectionOptions<K extends S.Key> {
  /** The element containing every item (both lists, for Trash). */
  scope: HTMLElement | null;
  /** The view in display order: every loaded item, rendered or not. */
  keys: readonly K[];
  columns?: number;
  /** Items in the view, loaded or not (server count); defaults to `keys.length`. */
  total?: number;
  onOpen?: (key: K) => void;
  /** Make the item at this index rendered (virtualized lists scroll to it). */
  reveal?: (index: number) => void;
  /** Selection clears when this changes (a different view). */
  resetOn?: string;
  /** Also take Cmd/Ctrl+A and Esc while nothing in particular has focus. */
  global?: boolean;
  /** Drag a rectangle from empty space to select (mouse only). */
  marquee?: boolean;
}

export interface Selection<K extends S.Key> {
  sel: S.Sel<K>;
  focus: K | null;
  count: number;
  /** Cmd/Ctrl+A with nothing deselected since: "All N selected". */
  allSelected: boolean;
  isSelected: (k: K) => boolean;
  /** Selected keys among the loaded ones, in view order. */
  selectedKeys: () => K[];
  /** Touch selection mode (after a long press, or the Select button): taps toggle. */
  selecting: boolean;
  setSelecting: (v: boolean) => void;
  selectAll: () => void;
  clear: () => void;
  /** Clear and leave selection mode. */
  exit: () => void;
  toggle: (k: K) => void;
  scopeProps: { onKeyDown: (e: React.KeyboardEvent) => void; onPointerDown: (e: React.PointerEvent) => void };
  cellProps: (k: K) => CellProps;
  marquee: CSSProperties | null;
}

export interface CellProps {
  "data-sel-key": string;
  role: "gridcell";
  tabIndex: number;
  "aria-selected": boolean;
  "data-focused"?: true;
  "data-selecting"?: true;
  onFocus: (e: React.FocusEvent) => void;
  onClick: (e: React.MouseEvent) => void;
  onDoubleClick: (e: React.MouseEvent) => void;
  onMouseDown: (e: React.MouseEvent) => void;
  onPointerDown: (e: React.PointerEvent) => void;
  onPointerMove: (e: React.PointerEvent) => void;
  onPointerUp: () => void;
  onPointerCancel: () => void;
  onContextMenu: (e: React.MouseEvent) => void;
}

interface Press {
  type: string;
  x: number;
  y: number;
  timer: number | null;
  long: boolean;
}

/** A control inside the item (not the item itself) received the event. */
function fromControl(e: { target: EventTarget; currentTarget: EventTarget }): boolean {
  const ctl = (e.target as Element).closest?.(INTERACTIVE);
  return !!ctl && ctl !== e.currentTarget && (e.currentTarget as Element).contains(ctl);
}

export function useSelection<K extends S.Key>(o: SelectionOptions<K>): Selection<K> {
  const [state, setState] = useState<S.SelState<K>>(S.initialState);
  const [selecting, setSelecting] = useState(false);
  const [marquee, setMarquee] = useState<CSSProperties | null>(null);
  const live = useRef({ o, state, selecting });
  live.current = { o, state, selecting };
  const pendingFocus = useRef(false);
  const press = useRef<Press | null>(null);
  const byString = useMemo(() => new Map(o.keys.map((k) => [String(k), k])), [o.keys]);

  useEffect(() => {
    setState(S.initialState());
  }, [o.resetOn]);
  useEffect(() => {
    setState((s) => {
      const sel = S.prune(s.sel, o.keys);
      const base = S.prune(s.base, o.keys);
      return sel === s.sel && base === s.base ? s : { ...s, sel, base };
    });
  }, [o.keys]);

  const total = o.total ?? o.keys.length;
  const count = S.selectedCount(state.sel, total);

  /** Commit a new state; with `moveFocus`, bring the focused item into view and focus it. */
  const apply = useCallback((next: S.SelState<K>, moveFocus = false) => {
    setState(next);
    if (moveFocus && next.focus !== null) {
      pendingFocus.current = true;
      const i = live.current.o.keys.indexOf(next.focus);
      if (i >= 0) live.current.o.reveal?.(i);
    }
  }, []);

  // After keyboard navigation, focus the new item once it is rendered (virtualized lists render
  // it after scrolling). Runs after every render of the owner, which also owns the window.
  useLayoutEffect(() => {
    if (!pendingFocus.current || state.focus === null || !o.scope) return;
    const el = o.scope.querySelector<HTMLElement>(`[data-sel-key="${CSS.escape(String(state.focus))}"]`);
    if (!el) return;
    pendingFocus.current = false;
    if (document.activeElement !== el) el.focus({ preventScroll: true });
    el.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  });

  const handleKey = useCallback((e: KeyboardEvent | React.KeyboardEvent, inScope: boolean) => {
    const { o, state, selecting } = live.current;
    if (foreignMod(e)) return;
    if (inScope) {
      const t = e.target as HTMLElement;
      const onItem = t.hasAttribute("data-sel-key");
      // Typing in a field, or using a control inside an item: theirs, except Esc to clear.
      if (!onItem && (isEditable(t) || e.key !== "Escape")) return;
    }
    const r = S.onKey(state, o.keys, o.columns ?? 1, e.key, { shift: e.shiftKey, mod: modOf(e), alt: e.altKey });
    if (!r) {
      if (e.key === "Escape" && selecting) {
        e.preventDefault();
        e.stopPropagation();
        setSelecting(false);
      }
      return;
    }
    e.preventDefault();
    e.stopPropagation();
    if (r.open !== undefined) return o.onOpen?.(r.open);
    if (e.key === "Escape") setSelecting(false);
    apply(r.state, r.state.focus !== state.focus);
  }, [apply]);

  // Cmd/Ctrl+A and Esc while focus is not on an item: anywhere on the page for Cmd/Ctrl+A
  // (except fields), and Esc only when nothing in particular is focused (menus keep theirs).
  // Capture phase, so Esc clears the selection before a sheet hears it and closes.
  useEffect(() => {
    if (!o.global) return;
    const onKey = (e: KeyboardEvent) => {
      const selectAllKey = (e.key === "a" || e.key === "A") && modOf(e) && !e.shiftKey && !e.altKey;
      if (!selectAllKey && e.key !== "Escape") return;
      const root = live.current.o.scope;
      if (!root?.isConnected) return;
      const a = document.activeElement as HTMLElement | null;
      if (a && root.contains(a)) return; // the scope's own handler has it
      if (isEditable(a)) return;
      const modal = document.querySelector('[aria-modal="true"]');
      if (modal ? !modal.contains(root) : root.closest("[inert]")) return;
      if (e.key === "Escape" && a && a !== document.body && a.id !== "main" && a !== modal) return;
      handleKey(e, false);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [o.global, handleKey]);

  const clearPress = () => {
    const p = press.current;
    if (p?.timer) window.clearTimeout(p.timer);
    if (p) p.timer = null;
  };

  const cellProps = (k: K): CellProps => {
    const tabbable = state.focus !== null && byString.has(String(state.focus)) ? state.focus : o.keys[0];
    return {
      "data-sel-key": String(k),
      role: "gridcell",
      tabIndex: k === tabbable ? 0 : -1,
      "aria-selected": S.isSelected(state.sel, k),
      "data-focused": state.focus === k ? true : undefined,
      "data-selecting": selecting ? true : undefined,
      onFocus: (e) => {
        if (e.target === e.currentTarget) setState((s) => (s.focus === k ? s : { ...s, focus: k }));
      },
      onPointerDown: (e) => {
        clearPress();
        const p: Press = { type: e.pointerType, x: e.clientX, y: e.clientY, timer: null, long: false };
        press.current = p;
        if (e.pointerType !== "touch" || fromControl(e)) return;
        p.timer = window.setTimeout(() => {
          p.timer = null;
          p.long = true;
          setSelecting(true);
          apply(S.add(live.current.state, k));
          navigator.vibrate?.(12);
        }, LONG_PRESS_MS);
      },
      onPointerMove: (e) => {
        const p = press.current;
        if (p?.timer && Math.hypot(e.clientX - p.x, e.clientY - p.y) > 10) clearPress();
      },
      onPointerUp: clearPress,
      onPointerCancel: clearPress,
      onContextMenu: (e) => {
        // A long press opens the browser's menu on touch; selection mode replaces it.
        if (press.current?.type === "touch") e.preventDefault();
      },
      onMouseDown: (e) => {
        if (e.shiftKey && !fromControl(e)) e.preventDefault(); // no text selection on Shift+click
      },
      onClick: (e) => {
        if (fromControl(e)) return;
        const p = press.current;
        press.current = null;
        if (p?.long) return; // the long press already selected it
        const { state, selecting, o } = live.current;
        const toggleHit = !!(e.target as Element).closest("[data-sel-toggle]");
        if (p?.type === "touch" || p?.type === "pen") {
          if (selecting || toggleHit) apply(S.toggle(state, k));
          else o.onOpen?.(k);
          return;
        }
        if (foreignMod(e) || e.detail > 1) return; // Ctrl+click on a Mac; 2nd click of a double-click
        if (toggleHit || (selecting && !e.shiftKey && !modOf(e))) return apply(S.toggle(state, k));
        apply(S.click(state, o.keys, k, { toggle: modOf(e), shift: e.shiftKey }));
      },
      onDoubleClick: (e) => {
        if (fromControl(e) || (e.target as Element).closest("[data-sel-toggle]")) return;
        if (live.current.selecting || e.shiftKey || modOf(e)) return;
        live.current.o.onOpen?.(k);
      },
    };
  };

  const onScopePointerDown = (e: React.PointerEvent) => {
    const root = o.scope;
    if (!o.marquee || !root || e.pointerType !== "mouse" || e.button !== 0) return;
    const t = e.target as Element;
    if (t.closest("[data-sel-key]") || t.closest(INTERACTIVE)) return;
    e.preventDefault(); // no text selection while dragging
    const additive = e.shiftKey || modOf(e);
    const startSel = additive ? live.current.state.sel : S.none<K>();
    const x0 = e.pageX;
    const y0 = e.pageY;
    let moved = false;
    const move = (ev: PointerEvent) => {
      if (!moved && Math.hypot(ev.pageX - x0, ev.pageY - y0) < 4) return;
      moved = true;
      const l = Math.min(x0, ev.pageX);
      const tp = Math.min(y0, ev.pageY);
      const r = Math.max(x0, ev.pageX);
      const b = Math.max(y0, ev.pageY);
      const box = root.getBoundingClientRect();
      const ox = box.left + window.scrollX;
      const oy = box.top + window.scrollY;
      setMarquee({ left: l - ox, top: tp - oy, width: r - l, height: b - tp });
      const hits: K[] = [];
      root.querySelectorAll<HTMLElement>("[data-sel-key]").forEach((el) => {
        const c = el.getBoundingClientRect();
        const cl = c.left + window.scrollX;
        const ct = c.top + window.scrollY;
        if (cl < r && cl + c.width > l && ct < b && ct + c.height > tp) {
          const key = byString.get(el.dataset.selKey ?? "");
          if (key !== undefined) hits.push(key);
        }
      });
      setState((s) => ({ sel: S.setSelected(startSel, hits, true), base: startSel, anchor: hits[0] ?? s.anchor, focus: hits[hits.length - 1] ?? s.focus }));
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      setMarquee(null);
      // A plain click on empty space clears, as on a desktop.
      if (!moved && !additive) setState((s) => S.clear(s));
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  return {
    sel: state.sel,
    focus: state.focus,
    count,
    allSelected: state.sel.all && state.sel.set.size === 0 && total > 0,
    isSelected: (k) => S.isSelected(state.sel, k),
    selectedKeys: () => S.selectedKeys(state.sel, o.keys),
    selecting,
    setSelecting,
    selectAll: () => apply(S.selectAll(live.current.state)),
    clear: () => apply(S.clear(live.current.state)),
    exit: () => {
      apply(S.clear(live.current.state));
      setSelecting(false);
    },
    toggle: (k) => apply(S.toggle(live.current.state, k)),
    scopeProps: { onKeyDown: (e) => handleKey(e, true), onPointerDown: onScopePointerDown },
    cellProps,
    marquee,
  };
}
