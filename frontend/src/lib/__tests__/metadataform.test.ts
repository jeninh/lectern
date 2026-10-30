// @vitest-environment jsdom
// The metadata form's issue #85 fields: subtitle, original publication date, and the contributors
// editor (name and role per row, add and remove, at most 20).
import { act, createElement, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { MetadataForm } from "../../components/MetadataForm";
import { buildPatch, initialDraft } from "../editor";
import type { EditorState, Metadata } from "../types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const meta: Metadata = {
  title: "Pedro Páramo",
  sort_title: null,
  authors: [{ name: "Juan Rulfo", sort: null }],
  series: null,
  series_index: null,
  description: null,
  language: "es",
  isbn: null,
  publisher: null,
  published: "1955",
  tags: [],
  narrators: [],
  subtitle: null,
  original_published: null,
  contributors: [],
  cover_font: null,
};

let host: HTMLDivElement;
let root: Root;
let latest: Metadata;
beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

function Harness({ start }: { start: Metadata }) {
  const [m, setM] = useState(start);
  latest = m;
  return createElement(MetadataForm, { value: m, onChange: setM });
}
const show = (start = meta) => act(() => root.render(createElement(Harness, { start })));
const type = (el: HTMLInputElement, v: string) =>
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, v);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
const byLabel = (text: string) => [...host.querySelectorAll("label")].find((l) => l.textContent?.startsWith(text))!.querySelector("input")!;
const addButton = () => [...host.querySelectorAll("button")].find((b) => b.textContent?.includes("Add contributor"))!;

describe("MetadataForm additions", () => {
  it("shows subtitle and first-published inputs with their values", () => {
    show({ ...meta, subtitle: "A novel", original_published: "1955-03" });
    expect(byLabel("Subtitle").value).toBe("A novel");
    expect(byLabel("First published").value).toBe("1955-03");
  });

  it("edits subtitle and first-published, and buildPatch carries them", () => {
    show();
    type(byLabel("Subtitle"), "A novel");
    type(byLabel("First published"), "1955");
    expect(latest.subtitle).toBe("A novel");
    const s = { version: 2, metadata: meta, shelves: [], tracks: [], files: [] } as unknown as EditorState;
    const patch = buildPatch(s, { ...initialDraft(s), metadata: latest });
    expect(patch.metadata).toMatchObject({ subtitle: "A novel", original_published: "1955" });
  });

  it("adds, fills, and removes contributor rows with a role picker", () => {
    show();
    expect(host.querySelectorAll(".contributor-row")).toHaveLength(0);
    act(() => addButton().click());
    act(() => addButton().click());
    expect(host.querySelectorAll(".contributor-row")).toHaveLength(2);
    const select = host.querySelector<HTMLSelectElement>("select")!;
    expect([...select.options].map((o) => o.value)).toEqual(["translator", "editor", "illustrator", "foreword"]);
    type(host.querySelector<HTMLInputElement>('input[aria-invalid][placeholder="Name"]')!, "Margaret Sayers Peden");
    act(() => {
      host.querySelectorAll<HTMLSelectElement>("select")[1].value = "illustrator";
      host.querySelectorAll<HTMLSelectElement>("select")[1].dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(latest.contributors).toEqual([{ name: "Margaret Sayers Peden", role: "translator" }, { name: "", role: "illustrator" }]);
    act(() => host.querySelector<HTMLButtonElement>('button[aria-label="Remove contributor Margaret Sayers Peden"]')!.click());
    expect(latest.contributors).toEqual([{ name: "", role: "illustrator" }]);
    // Empty rows never reach the server.
    const s = { version: 2, metadata: meta, shelves: [], tracks: [], files: [] } as unknown as EditorState;
    expect(buildPatch(s, { ...initialDraft(s), metadata: latest }).metadata).toBeUndefined();
  });

  it("stops adding at 20 contributors", () => {
    show({ ...meta, contributors: Array.from({ length: 19 }, (_, i) => ({ name: `P${i}`, role: "editor" as const })) });
    expect(addButton().disabled).toBe(false);
    act(() => addButton().click());
    expect(latest.contributors).toHaveLength(20);
    expect(addButton().disabled).toBe(true);
  });
});
