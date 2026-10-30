import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { clauseLabel, extractChips, FIELDS, FilterError, formatQuery, parse, SORTS } from "../filters";

// The cases lectern.filters is held to as well (tests/test_filters.py).
const fixtures = join(__dirname, "../../../../tests/fixtures");
const vectors: { input: string; canonical?: string; error?: string }[] = JSON.parse(readFileSync(join(fixtures, "filters.json"), "utf8")).cases;
const catalog = JSON.parse(readFileSync(join(fixtures, "filter_fields.json"), "utf8"));

describe("filter syntax", () => {
  for (const c of vectors) {
    it(JSON.stringify(c.input), () => {
      if (c.error) {
        let err: unknown = null;
        try {
          parse(c.input);
        } catch (e) {
          err = e;
        }
        expect(err).toBeInstanceOf(FilterError);
        expect((err as FilterError).code).toBe(c.error);
      } else {
        const out = formatQuery(parse(c.input));
        expect(out).toBe(c.canonical);
        expect(formatQuery(parse(out))).toBe(out);
      }
    });
  }

  it("has the server's fields, options, aliases, and sorts", () => {
    const mine = FIELDS.map(({ key, kind, label, group, aliases, options, members_only }) => ({
      key, kind, label, group, aliases, members_only, options: options.map(({ value, label }) => ({ value, label })),
    }));
    expect(mine).toEqual(catalog.fields.map(({ key, kind, label, group, aliases, options, members_only }: (typeof mine)[number]) => ({ key, kind, label, group, aliases, members_only, options })));
    const aliases = Object.fromEntries(FIELDS.filter((x) => x.options.length).map((x) => [x.key, Object.fromEntries(x.options.map((op) => [op.value, op.aliases]))]));
    expect(aliases).toEqual(catalog.aliases);
    expect(SORTS).toEqual(catalog.sorts);
  });
});

describe("typed syntax becomes chips", () => {
  it("takes finished tokens and leaves the one being typed", () => {
    const r = extractChips("format:epub war -has:audio auth", false);
    expect(r.clauses.map((c) => c.key)).toEqual(["format", "has"]);
    expect(r.clauses[1].negate).toBe(true);
    expect(r.rest).toBe("war auth");
    expect(extractChips("format:ep", false).clauses).toEqual([]);
    expect(extractChips("format:epub", true).rest).toBe("");
  });

  it("keeps a token with a bad value in the box and says why", () => {
    const r = extractChips("format:mobi tolstoy ", false);
    expect(r.clauses).toEqual([]);
    expect(r.rest).toBe("format:mobi tolstoy ");
    expect(r.error?.code).toBe("unknown_value");
  });

  it("leaves unknown keys and quoted colons as words", () => {
    expect(extractChips('Re:Zero "a:b" ', false)).toEqual({ clauses: [], rest: 'Re:Zero "a:b" ', error: null });
  });
});

describe("chip labels", () => {
  const label = (f: string, names?: Record<string, string>) => {
    const { field, values } = clauseLabel(parse(f).clauses[0], (_k, ref) => names?.[ref]);
    return `${field}: ${values}`;
  };
  it("reads like the issue", () => {
    expect(label("format:epub,pdf")).toBe("Format: EPUB or PDF");
    expect(label("-has:audio")).toBe("Has audiobook: no");
    expect(label("-format:pdf")).toBe("Format: is not PDF");
    expect(label("progress:<50%")).toBe("Progress: under 50%");
    expect(label("year:1850..1900")).toBe("Year: 1850–1900");
    expect(label("size:>100mb")).toBe("File size: over 100 MB");
    expect(label("length:1h30m..")).toBe("Audio length: at least 1 h 30 m");
    expect(label("added:<30d")).toBe("Added: in the last 30 days");
    expect(label("read:>1y")).toBe("Last read: more than 1 year ago");
    expect(label("finished:2026-03")).toBe("Finished: 2026-03");
    expect(label("shelf:#4,none", { "#4": "Kids / Picture books" })).toBe("Shelf: Kids / Picture books or not on a shelf");
    expect(label("shelf:#9")).toBe("Shelf: a deleted shelf");
    expect(label("is:duplicate")).toBe("Possible duplicate: yes");
    expect(label("needs:ocr")).toBe("Needs OCR: yes");
  });
});

describe("exact text values", () => {
  it("parses = as \"is exactly\" and keeps it when formatted", () => {
    const q = parse('-author:="Leo Tolstoy",=Pushkin tag:=sf author:"=X"');
    expect(q.clauses[0].values).toEqual([{ op: "==", a: "Leo Tolstoy", b: "" }, { op: "==", a: "Pushkin", b: "" }]);
    expect(q.clauses[1].values).toEqual([{ op: "=", a: "sf", b: "" }]);
    expect(q.clauses[2].values).toEqual([{ op: "=", a: "=X", b: "" }]);
    expect(formatQuery(q)).toBe('-author:=\"Leo Tolstoy\",=Pushkin tag:sf author:\"=X\"');
  });

  it("renders the \"is exactly\" chip", () => {
    expect(clauseLabel(parse("author:=Tolstoy").clauses[0])).toEqual({ field: "Author", values: "is exactly Tolstoy" });
    expect(clauseLabel(parse("-author:=Tolstoy").clauses[0])).toEqual({ field: "Author", values: "is not exactly Tolstoy" });
    expect(clauseLabel(parse("author:Tolstoy").clauses[0])).toEqual({ field: "Author", values: "Tolstoy" });
  });
});
