import { describe, expect, it } from "vitest";
import { click, initialState, isSelected, navTarget, onKey, prune, selectedCount, selectedKeys, type SelState } from "../selection";

const keys = Array.from({ length: 10 }, (_, i) => i);
const picked = (s: SelState<number>) => selectedKeys(s.sel, keys);
const plain = { toggle: false, shift: false };
const press = (s: SelState<number>, key: string, m: { shift?: boolean; mod?: boolean } = {}, cols = 4) => {
  const r = onKey(s, keys, cols, key, { shift: !!m.shift, mod: !!m.mod });
  if (!r) throw new Error(`${key} not handled`);
  return r.state;
};

describe("navTarget in a 4-column grid of 10 (rows 0-3, 4-7, 8-9)", () => {
  it.each([
    ["ArrowRight", 3, false, 4],
    ["ArrowRight", 9, false, 9],
    ["ArrowLeft", 4, false, 3],
    ["ArrowDown", 1, false, 5],
    // the row below is short: land on the last item, not past it
    ["ArrowDown", 6, false, 9],
    ["ArrowDown", 8, false, 8],
    ["ArrowUp", 9, false, 5],
    ["ArrowUp", 2, false, 2],
    ["Home", 6, false, 4],
    ["End", 5, false, 7],
    ["End", 8, false, 9],
    ["ArrowLeft", 6, true, 4],
    ["ArrowRight", 4, true, 7],
    ["ArrowRight", 8, true, 9],
    ["ArrowUp", 7, true, 0],
    ["ArrowDown", 1, true, 9],
    ["Home", 6, true, 0],
    ["End", 2, true, 9],
  ])("%s from %i (jump %s) → %i", (key, from, jump, to) => {
    expect(navTarget(10, 4, from, key, jump)).toBe(to);
  });

  it("treats Home and End in a one-column list as the ends of the list", () => {
    expect(navTarget(10, 1, 5, "Home", false)).toBe(0);
    expect(navTarget(10, 1, 5, "End", false)).toBe(9);
    expect(navTarget(10, 1, 5, "ArrowDown", false)).toBe(6);
  });

  it("ignores other keys", () => {
    expect(navTarget(10, 4, 0, "PageDown", false)).toBeNull();
  });
});

describe("pointer selection", () => {
  it("Shift replaces with the range; Cmd/Ctrl+Shift adds it to what was picked before", () => {
    let s = click(initialState<number>(), keys, 1, plain);
    s = click(s, keys, 6, { toggle: true, shift: false }); // anchor moves to 6
    expect(picked(s)).toEqual([1, 6]);
    expect(picked(click(s, keys, 8, { toggle: false, shift: true }))).toEqual([6, 7, 8]);
    s = click(s, keys, 8, { toggle: true, shift: true });
    expect(picked(s)).toEqual([1, 6, 7, 8]);
    // A second additive range from the same anchor replaces the first range, not the base.
    s = click(s, keys, 4, { toggle: true, shift: true });
    expect(picked(s)).toEqual([1, 4, 5, 6]);
  });

  it("toggles off with Cmd/Ctrl and keeps the rest", () => {
    let s = click(initialState<number>(), keys, 2, plain);
    s = click(s, keys, 2, { toggle: true, shift: false });
    expect(picked(s)).toEqual([]);
  });
});

describe("keyboard selection", () => {
  it("arrows move focus only; Space toggles; Enter opens", () => {
    let s = press(initialState<number>(), "ArrowRight");
    expect(s.focus).toBe(0); // first key into the collection lands on the first item
    s = press(s, "ArrowDown");
    expect(s.focus).toBe(4);
    expect(picked(s)).toEqual([]);
    s = press(s, " ");
    expect(picked(s)).toEqual([4]);
    expect(onKey(s, keys, 4, "Enter", { shift: false, mod: false })?.open).toBe(4);
  });

  it("Shift+Arrow grows and shrinks from the anchor and keeps earlier picks", () => {
    let s = click(initialState<number>(), keys, 0, plain);
    s = press(s, "ArrowRight");
    s = press(s, "ArrowRight"); // focus 2, nothing new selected
    s = press(s, "ArrowRight", { shift: true });
    s = press(s, "ArrowRight", { shift: true });
    expect(picked(s)).toEqual([0, 2, 3, 4]);
    s = press(s, "ArrowLeft", { shift: true });
    expect(picked(s)).toEqual([0, 2, 3]);
    s = press(s, "ArrowDown", { shift: true }); // 3 → 7
    expect(picked(s)).toEqual([0, 2, 3, 4, 5, 6, 7]);
    s = press(s, "Home", { shift: true }); // row start of 7 is 4
    expect(picked(s)).toEqual([0, 2, 3, 4]);
    s = press(s, "End", { shift: true, mod: true });
    expect(picked(s)).toEqual([0, 2, 3, 4, 5, 6, 7, 8, 9]);
  });

  it("Escape clears, and is not ours when nothing is selected", () => {
    let s = click(initialState<number>(), keys, 3, plain);
    s = press(s, "Escape");
    expect(picked(s)).toEqual([]);
    expect(onKey(s, keys, 4, "Escape", { shift: false, mod: false })).toBeNull();
  });

  it("leaves Cmd/Ctrl+K and other shortcuts alone", () => {
    const s = initialState<number>();
    expect(onKey(s, keys, 4, "k", { shift: false, mod: true })).toBeNull();
    expect(onKey(s, keys, 4, "a", { shift: false, mod: false })).toBeNull();
  });
});

describe("select all", () => {
  it("counts the whole view, loaded or not, minus what was deselected", () => {
    let s = press(initialState<number>(), "a", { mod: true });
    expect(selectedCount(s.sel, 300)).toBe(300);
    s = click(s, keys, 5, { toggle: true, shift: false });
    expect(isSelected(s.sel, 5)).toBe(false);
    expect(selectedCount(s.sel, 300)).toBe(299);
    // Shift+Arrow while everything is selected keeps "all" and re-adds the range.
    s = press(s, "ArrowLeft", { shift: true });
    expect(selectedCount(s.sel, 300)).toBe(300);
  });

  it("prunes keys that left the view so counts stay honest", () => {
    let s = click(initialState<number>(), keys, 1, plain);
    s = click(s, keys, 2, { toggle: true, shift: false });
    expect(selectedCount(prune(s.sel, [2, 3]), 2)).toBe(1);
  });
});
