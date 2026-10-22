// Desktop-style multi-select over an ordered collection, as pure functions (useSelection wires
// them to the DOM). Conventions follow Finder and Explorer:
//
// - click selects one item; Cmd (Mac) / Ctrl (elsewhere) + click toggles one;
// - Shift + click selects anchor..item, replacing the selection;
// - Cmd/Ctrl + Shift + click adds anchor..item to what was selected when the anchor was set;
// - arrows move focus without changing the selection (Space toggles, Enter opens);
//   Shift + arrow extends from the anchor, keeping what was selected before the range began;
// - Cmd/Ctrl + A selects the whole view, including items that are not loaded: the selection
//   becomes "all except" and the server resolves it (lectern.routes.api.selection).

export type Key = string | number;

/** `all: false`: exactly `set` is selected. `all: true`: everything except `set`. */
export interface Sel<K extends Key> {
  all: boolean;
  set: ReadonlySet<K>;
}

export interface SelState<K extends Key> {
  sel: Sel<K>;
  /** Fixed end of Shift ranges. */
  anchor: K | null;
  /** The item keyboard focus is on (roving tabindex). */
  focus: K | null;
  /** What was selected when the anchor was set; Shift ranges are added to it. */
  base: Sel<K>;
}

export const none = <K extends Key>(): Sel<K> => ({ all: false, set: new Set() });
export const everything = <K extends Key>(): Sel<K> => ({ all: true, set: new Set() });
export const initialState = <K extends Key>(): SelState<K> => ({ sel: none(), anchor: null, focus: null, base: none() });

export function isSelected<K extends Key>(sel: Sel<K>, k: K): boolean {
  return sel.all !== sel.set.has(k);
}

/** How many are selected when the view holds `total` items (loaded or not). */
export function selectedCount<K extends Key>(sel: Sel<K>, total: number): number {
  return sel.all ? Math.max(0, total - sel.set.size) : sel.set.size;
}

export function selectedKeys<K extends Key>(sel: Sel<K>, keys: readonly K[]): K[] {
  return keys.filter((k) => isSelected(sel, k));
}

export function setSelected<K extends Key>(sel: Sel<K>, ks: Iterable<K>, on: boolean): Sel<K> {
  const set = new Set(sel.set);
  // A key is in `set` exactly when its state differs from the default (`all`).
  for (const k of ks) {
    if (on !== sel.all) set.add(k);
    else set.delete(k);
  }
  return { all: sel.all, set };
}

/** Drops keys that left the view, so counts stay true after items are filed or trashed. */
export function prune<K extends Key>(sel: Sel<K>, keys: readonly K[]): Sel<K> {
  const present = new Set(keys);
  if ([...sel.set].every((k) => present.has(k))) return sel;
  return { all: sel.all, set: new Set([...sel.set].filter((k) => present.has(k))) };
}

export function range<K extends Key>(keys: readonly K[], a: K, b: K): K[] {
  const i = keys.indexOf(a);
  const j = keys.indexOf(b);
  if (j < 0) return [];
  if (i < 0) return [b];
  return keys.slice(Math.min(i, j), Math.max(i, j) + 1);
}

export interface ClickMods {
  /** Cmd on a Mac, Ctrl elsewhere. */
  toggle: boolean;
  shift: boolean;
}

export function click<K extends Key>(s: SelState<K>, keys: readonly K[], k: K, m: ClickMods): SelState<K> {
  if (m.shift && s.anchor !== null && keys.includes(s.anchor)) {
    const start = m.toggle ? s.base : none<K>();
    return { ...s, sel: setSelected(start, range(keys, s.anchor, k), true), focus: k };
  }
  if (m.toggle) return toggle(s, k);
  const sel: Sel<K> = { all: false, set: new Set([k]) };
  return { sel, anchor: k, focus: k, base: none() };
}

export function toggle<K extends Key>(s: SelState<K>, k: K): SelState<K> {
  const sel = setSelected(s.sel, [k], !isSelected(s.sel, k));
  return { sel, anchor: k, focus: k, base: sel };
}

/** Selects `k` (long press on touch), keeping the rest. */
export function add<K extends Key>(s: SelState<K>, k: K): SelState<K> {
  const sel = setSelected(s.sel, [k], true);
  return { sel, anchor: k, focus: k, base: sel };
}

export function selectAll<K extends Key>(s: SelState<K>): SelState<K> {
  return { ...s, sel: everything(), base: everything() };
}

export function clear<K extends Key>(s: SelState<K>): SelState<K> {
  return { ...s, sel: none(), base: none() };
}

/** Where a navigation key moves focus in a grid of `n` items laid out in `cols` columns;
 * null when the key is not a navigation key. `jump` is Cmd (Mac) / Ctrl (elsewhere). */
export function navTarget(n: number, cols: number, i: number, key: string, jump: boolean): number | null {
  if (n === 0) return null;
  const last = n - 1;
  const c = Math.max(1, cols);
  const rowStart = i - (i % c);
  const rowEnd = Math.min(rowStart + c - 1, last);
  // In a one-column list a row is one item, so Home/End go to the ends of the list.
  const toFirst = jump || c === 1;
  switch (key) {
    case "ArrowRight":
      return jump ? rowEnd : Math.min(i + 1, last);
    case "ArrowLeft":
      return jump ? rowStart : Math.max(i - 1, 0);
    case "ArrowDown":
      if (jump) return last;
      if (i + c <= last) return i + c;
      // From a full row above a short last row, land on the last item (Finder does too).
      return Math.floor(i / c) < Math.floor(last / c) ? last : i;
    case "ArrowUp":
      if (jump) return 0;
      return i - c >= 0 ? i - c : i;
    case "Home":
      return toFirst ? 0 : rowStart;
    case "End":
      return toFirst ? last : rowEnd;
    default:
      return null;
  }
}

export interface KeyMods {
  shift: boolean;
  /** Cmd on a Mac, Ctrl elsewhere. */
  mod: boolean;
  alt?: boolean;
}

export type KeyResult<K extends Key> = { state: SelState<K>; open?: K } | null;

/** The new state for a key press on the collection, or null when the key is not ours. */
export function onKey<K extends Key>(s: SelState<K>, keys: readonly K[], cols: number, key: string, m: KeyMods): KeyResult<K> {
  if (m.alt) return null;
  const i = s.focus !== null ? keys.indexOf(s.focus) : -1;
  if ((key === "a" || key === "A") && m.mod && !m.shift) return { state: selectAll(s) };
  if (key === "Escape") return selectedCount(s.sel, keys.length) > 0 || s.sel.all ? { state: clear(s) } : null;
  if (m.mod && !["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(key)) return null;
  if (key === "Enter") return i >= 0 && !m.shift ? { state: s, open: keys[i] } : null;
  if (key === " " || key === "Spacebar") {
    if (i < 0) return keys.length ? { state: { ...s, focus: keys[0], anchor: keys[0], base: s.sel } } : null;
    const f = keys[i];
    if (m.shift && s.anchor !== null) return { state: { ...s, sel: setSelected(s.base, range(keys, s.anchor, f), true) } };
    return { state: toggle(s, f) };
  }
  const j = navTarget(keys.length, cols, Math.max(i, 0), key, m.mod);
  if (j === null) return null;
  // First key press into a collection without focus lands on the first item.
  const target = i < 0 ? keys[0] : keys[j];
  if (!m.shift) return { state: { ...s, focus: target, anchor: target, base: s.sel } };
  const anchor = s.anchor !== null && keys.includes(s.anchor) ? s.anchor : keys[Math.max(i, 0)];
  const base = s.anchor !== null && keys.includes(s.anchor) ? s.base : s.sel;
  return { state: { sel: setSelected(base, range(keys, anchor, target), true), anchor, focus: target, base } };
}
