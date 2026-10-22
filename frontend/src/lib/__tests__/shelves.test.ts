import { describe, expect, it } from "vitest";
import { ancestry, canHoldBooks, childrenOf, dropMode, height, moveBlocked, splitKeys, worksIn } from "../shelves";
import type { Shelf, Work } from "../types";

// Kids(1) > Picture(2) > Board(3); Kids > Chapter(4); Classics(5, view only); Deep: L1..L7
// (10..16), so L7 is at depth 7.
function shelf(id: number, name: string, parent: number | null, permission: Shelf["permission"] = "owner"): Shelf {
  return { id, name, parent_id: parent, path: [], owner: "o", owner_id: 1, permission, work_count: 0, shelf_count: 0, grant_count: 0, smart: false };
}
const deep = Array.from({ length: 7 }, (_, i) => shelf(10 + i, `L${i + 1}`, i === 0 ? null : 9 + i));
const all: Shelf[] = [shelf(1, "Kids", null), shelf(2, "Picture", 1), shelf(3, "Board", 2), shelf(4, "Chapter", 1), shelf(5, "Classics", null, "view"), ...deep];

describe("tree", () => {
  it("children in list order, ancestry top first", () => {
    expect(childrenOf(all, 1).map((s) => s.id)).toEqual([2, 4]);
    expect(childrenOf(all, null).map((s) => s.id)).toEqual([1, 5, 10]);
    expect(ancestry(all, 3).map((s) => s.name)).toEqual(["Kids", "Picture", "Board"]);
    expect(height(all, 1)).toBe(3);
    expect(height(all, 4)).toBe(1);
  });
});

describe("moveBlocked", () => {
  it("never into itself or below itself", () => {
    expect(moveBlocked(all, [1], 1, true)).toMatch(/into itself/);
    expect(moveBlocked(all, [1], 3, true)).toMatch(/inside it/);
    expect(moveBlocked(all, [2], 4, true)).toBeNull(); // a sibling is fine
    expect(moveBlocked(all, [3], null, true)).toBeNull();
    expect(moveBlocked(all, [3], null, false)).toMatch(/top-level/);
  });
  it("not into a view-only shelf, and only shelves you own move", () => {
    expect(moveBlocked(all, [4], 5, true)).toMatch(/can’t add to Classics/);
    expect(moveBlocked(all, [5], 1, true)).toMatch(/Only its owner/);
  });
  it("depth: target depth plus the moved subtree's height at most 8", () => {
    // L7 is at depth 7: a leaf fits (8), Kids (height 3) does not (10); L6 + leaf = 7.
    expect(moveBlocked(all, [4], 16, true)).toBeNull();
    expect(moveBlocked(all, [2], 16, true)).toMatch(/at most 8/); // Picture has Board: 9
    expect(moveBlocked(all, [2], 15, true)).toBeNull(); // 6 + 2 = 8
  });
});

describe("folders hold books", () => {
  const w = (id: number, shelf_ids: number[]) => ({ id, shelf_ids }) as unknown as Work;
  const works = [w(1, []), w(2, [1]), w(3, [1, 4]), w(4, [4])];
  it("root is the books on no shelf; a shelf its direct books (several shelves allowed)", () => {
    expect(worksIn(works, null).map((x) => x.id)).toEqual([1]);
    expect(worksIn(works, 1).map((x) => x.id)).toEqual([2, 3]);
    expect(worksIn(works, 4).map((x) => x.id)).toEqual([3, 4]);
    expect(worksIn(works, 2)).toEqual([]);
  });
  it("view-only shelves take no books; the root always does", () => {
    expect(canHoldBooks(all, 5)).toBe(false);
    expect(canHoldBooks(all, 1)).toBe(true);
    expect(canHoldBooks(all, null)).toBe(true);
  });
  it("Alt/Option adds, a plain drop moves; keys split by kind", () => {
    expect(dropMode({ altKey: true })).toBe("add");
    expect(dropMode({ altKey: false })).toBe("move");
    expect(splitKeys(["s1", "w20", "s3", "w4"])).toEqual({ shelves: [1, 3], works: [20, 4] });
  });
});
