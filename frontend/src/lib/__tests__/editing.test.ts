import { describe, expect, it } from "vitest";
import { buildPatch, changes, initialDraft, rebase } from "../editor";
import { applyFields, clean, normalizeIsbn, sortName, suggestionDiff, validate } from "../metadata";
import type { EditorState, Metadata } from "../types";

const meta = (over: Partial<Metadata> = {}): Metadata => ({
  title: "A Wizard of Earthsea",
  sort_title: null,
  authors: [{ name: "Ursula K. Le Guin", sort: "Le Guin, Ursula K." }],
  series: "Earthsea",
  series_index: 1,
  description: null,
  language: "en",
  isbn: null,
  publisher: null,
  published: "1968",
  tags: ["Fantasy"],
  narrators: [],
  ...over,
});

function state(over: Partial<EditorState> = {}): EditorState {
  return {
    work: { id: 5 } as EditorState["work"],
    version: 3,
    metadata: meta(),
    playback_default: null,
    readalong_enabled: true,
    primary_file_id: 10,
    cover: { has_cover: true, url: "/c.jpg", source: "embedded", embedded_available: true },
    files: [
      { id: 10, kind: "ebook", rel_path: "a.epub", size: 1, sha256: "x", partial_md5: null, added_at: null, primary: true },
      { id: 11, kind: "ebook", rel_path: "b.epub", size: 1, sha256: "y", partial_md5: null, added_at: null, primary: false },
    ],
    tracks: [
      { id: 1, position: 0, title: "Part 1", rel_path: "01.mp3", duration_s: 60, size: 1, codec: "mp3", sha256: "a", chapters: [{ title: "One", start_s: 0 }] },
      { id: 2, position: 1, title: "Part 2", rel_path: "02.mp3", duration_s: 60, size: 1, codec: "mp3", sha256: "b", chapters: [] },
    ],
    shelves: [
      { id: 7, name: "Kids", member: true },
      { id: 8, name: "Classics", member: false },
    ],
    alignment: null,
    align_models: ["base"],
    align_default_model: "base",
    read_aloud: {} as EditorState["read_aloud"],
    folder: { dir_path: "x", pending: false },
    last_revision: null,
    lookup_enabled: false,
    ...over,
  };
}

describe("metadata rules mirror the server", () => {
  it("accepts ISBNs only with a valid check digit, with or without hyphens", () => {
    expect(normalizeIsbn("978-0-547-77374-2")).toBe("9780547773742");
    expect(normalizeIsbn("9780547773743")).toBeNull();
    expect(normalizeIsbn("0-306-40615-2")).toBe("0306406152");
    expect(normalizeIsbn("080442957X")).toBe("080442957X");
    expect(normalizeIsbn("0306406153")).toBeNull();
  });

  it("names the field to fix", () => {
    expect(validate(meta())).toEqual({});
    const e = validate(meta({ title: " ", language: "english!", published: "68", isbn: "123", authors: [{ name: "" }] }));
    expect(Object.keys(e).sort()).toEqual(["authors", "isbn", "language", "published", "title"]);
  });

  it("sorts names by surname, keeping particles", () => {
    expect(sortName("Ursula K. Le Guin")).toBe("Le Guin, Ursula K.");
    expect(sortName("Ludwig van Beethoven")).toBe("van Beethoven, Ludwig");
    expect(sortName("Plato")).toBe("Plato");
    expect(sortName("Tolkien, J. R. R.")).toBe("Tolkien, J. R. R.");
  });
});

describe("metadata suggestions are applied only field by field", () => {
  const current = meta({ description: "Mine", isbn: null });
  const suggested: Partial<Metadata> = { title: "A Wizard of Earthsea", description: "Theirs", isbn: "9780547773742", publisher: "Parnassus", tags: [], authors: [{ name: "Ursula K. Le Guin" }] };

  it("lists only fields that would change, never empty or equal ones", () => {
    expect(suggestionDiff(current, suggested)).toEqual(["description", "isbn", "publisher"]);
  });

  it("changes exactly the chosen fields and leaves the rest as the person had them", () => {
    const next = applyFields(current, suggested, ["isbn"]);
    expect(next).toEqual({ ...current, isbn: "9780547773742" });
    expect(next.description).toBe("Mine");
  });
});

describe("editor draft", () => {
  it("starts clean and builds an empty patch", () => {
    const s = state();
    const d = initialDraft(s);
    expect(changes(s, d)).toEqual([]);
    expect(buildPatch(s, d)).toEqual({ version: 3 });
  });

  it("sends only what changed, with the version it was based on", () => {
    const s = state();
    const d = { ...initialDraft(s), metadata: meta({ series_index: 2 }), shelves: [8, 7] };
    expect(buildPatch(s, d)).toEqual({ version: 3, metadata: clean(meta({ series_index: 2 })), shelves: [7, 8] });
    expect(changes(s, d)).toEqual(["series index", "shelves"]);
  });

  it("whitespace-only edits are not changes", () => {
    const s = state();
    const d = { ...initialDraft(s), metadata: meta({ title: "A Wizard of Earthsea  ", publisher: "  " }) };
    expect(changes(s, d)).toEqual([]);
  });

  it("sends the full new track order with only edited titles and chapters, without removed tracks", () => {
    const s = state();
    const base = initialDraft(s);
    const d = {
      ...base,
      tracks: [{ ...base.tracks[1], title: "Second half" }, { ...base.tracks[0], chapters: ["Opening"] }],
      remove_track_ids: [],
    };
    expect(buildPatch(s, d).tracks).toEqual([{ id: 2, title: "Second half" }, { id: 1, chapters: ["Opening"] }]);
    const removing = { ...base, remove_track_ids: [2] };
    expect(buildPatch(s, removing)).toEqual({ version: 3, remove_track_ids: [2] });
  });

  it("never asks to make a removed EPUB primary", () => {
    const s = state();
    const d = { ...initialDraft(s), primary_file_id: 11, remove_file_ids: [11] };
    expect(buildPatch(s, d)).toEqual({ version: 3, remove_file_ids: [11] });
  });

  it("after a conflict keeps the person's own edits on top of the newer save", () => {
    const old = state();
    const mine = { ...initialDraft(old), metadata: meta({ series_index: 2 }) };
    const theirs = state({ version: 4, metadata: meta({ description: "Added by someone else", series_index: 1 }), shelves: [{ id: 7, name: "Kids", member: false }, { id: 8, name: "Classics", member: false }] });
    const merged = rebase(old, theirs, mine);
    expect(merged.metadata.series_index).toBe(2);
    expect(merged.metadata.description).toBe("Added by someone else");
    expect(merged.shelves).toEqual([]);
    expect(buildPatch(theirs, merged)).toEqual({ version: 4, metadata: clean({ ...theirs.metadata, series_index: 2 }) });
  });
});

describe("review hardening", () => {
  it("flags XML-illegal control characters per field but allows tabs and newlines", () => {
    expect(validate(meta({ title: "Bad\u0001Title" })).title).toMatch(/control characters/);
    expect(validate(meta({ authors: [{ name: "A\u0000", sort: null }] })).authors).toMatch(/control characters/);
    expect(validate(meta({ tags: ["ok", "bell\u0007"] })).tags).toMatch(/control characters/);
    expect(validate(meta({ description: "line\nnext\r\n\tindent" }))).toEqual({});
  });

  it("summarises per-book bulk results in plain language", async () => {
    const { bulkSummary } = await import("../editor");
    const s = bulkSummary("Undid", [
      { ok: true },
      { ok: false, error: "undo_conflict" },
      { ok: false, error: "io_error" },
      { ok: false, error: "io_error" },
    ]);
    expect(s.ok).toBe(1);
    expect(s.failed).toBe(3);
    expect(s.text).toBe(
      "Undid 1 book; 3 books could not be changed (This book changed after that save, so it can no longer be undone.; a file could not be moved on the server).",
    );
    expect(bulkSummary("Changed", [{ ok: true }, { ok: true }]).text).toBe("Changed 2 books.");
  });
});
