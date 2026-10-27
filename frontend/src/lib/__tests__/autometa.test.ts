import { describe, expect, it } from "vitest";
import { duplicateBanner, duplicateCopy, fixSummary, mergeNote, offered, statusText, toggle } from "../autometa";
import type { MetaSuggestionItem } from "../types";

const item = (over: Partial<MetaSuggestionItem>): MetaSuggestionItem => ({
  work_id: 1,
  version: 3,
  title: "Kim",
  author: "Rudyard Kipling",
  status: "ready",
  error: null,
  fields: {},
  current: { description: null, series: null, series_index: null, isbn: null, tags: [] },
  updated_at: null,
  ...over,
});
const s = (value: unknown) => ({ value, provider: "openlibrary", provider_name: "Open Library", source_url: "https://openlibrary.org/" });

describe("fixSummary (the Fix metadata bulk action)", () => {
  it("summarises per-book results from a selection", () => {
    const r = fixSummary(
      [{ status: "queued" }, { status: "queued" }, { status: "nothing_missing" }, { status: "already_queued" }, { status: "not_found" }],
      {},
    );
    expect(r.queued).toBe(2);
    expect(r.text).toBe("Looking up 2 books; suggestions appear for review as they arrive · 1 already queued · 1 book with nothing missing · 1 not found");
  });

  it("uses counts for a whole-library pass, which returns no per-book list", () => {
    expect(fixSummary(null, { queued: 1, dismissed: 4 }).text).toBe("Looking up 1 book; suggestions appear for review as they arrive · 4 you dismissed before (select them to ask again)");
    expect(fixSummary([], {})).toEqual({ text: "Nothing to look up", queued: 0 });
  });
});

describe("metadata review", () => {
  it("offers suggested fields in display order", () => {
    const it1 = item({ fields: { tags: s(["Spies"]), description: s("A boy in India."), isbn: s("9780141442372") } });
    expect(offered(it1)).toEqual(["description", "isbn", "tags"]);
  });

  it("toggles a field and keeps the display order", () => {
    expect(toggle(["tags"], "description")).toEqual(["description", "tags"]);
    expect(toggle(["description", "tags"], "description")).toEqual(["tags"]);
  });

  it("explains results that are not suggestions", () => {
    expect(statusText(item({ status: "queued" }))).toBe("Looking up…");
    expect(statusText(item({ status: "error", error: "lookup_offline" }))).toMatch(/offline mode/);
    expect(statusText(item({ status: "error", error: "something_new" }))).toMatch(/failed/);
    expect(statusText(item({ status: "ready", fields: { series: s("Raj") } }))).toBe("1 suggestion");
  });
});

describe("duplicate review", () => {
  it("words library pairs and upload matches differently", () => {
    expect(duplicateCopy({ origin: "library" }).keep).toBe("Not duplicates — ignore");
    expect(duplicateCopy({ origin: "ingest" }).keep).toBe("Different books");
    expect(duplicateCopy({}).question).toMatch(/new upload/); // rows from before the scan existed
  });

  it("says which copy a library merge keeps and what it discards", () => {
    const note = mergeNote({ origin: "library", work: { title: "Kim (2)" }, candidate: { title: "Kim" } });
    expect(note).toMatch(/^Merging keeps “Kim”\. “Kim \(2\)” goes/);
    expect(note).toMatch(/fill only fields the kept book leaves empty/);
    expect(note).toMatch(/Discarded with it: .*edit history/);
    expect(mergeNote({ origin: "ingest", work: { title: "a" }, candidate: { title: "b" } })).toBeNull();
    expect(duplicateCopy({ origin: "library" }).keptLabel).toBe("kept");
  });

  it("counts each kind in the banner", () => {
    expect(duplicateBanner([{ origin: "library" }])).toBe("One pair of books looks like duplicates");
    expect(duplicateBanner([{ origin: "ingest" }, { origin: "library" }, { origin: "library" }])).toBe(
      "One upload might belong to a book you already have; 2 pairs of books look like duplicates",
    );
  });
});
