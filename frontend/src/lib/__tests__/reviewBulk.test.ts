import { describe, expect, it } from "vitest";
import { bulkPlan } from "../reviewBulk";
import type { ImportInfo, Metadata, Proposal } from "../types";

const meta = { title: "Dune", authors: [{ name: "Frank Herbert" }], narrators: [], tags: [] } as unknown as Metadata;
const join = (confidence: Proposal["confidence"]): Proposal => ({ action: "join", work_id: 7, confidence, score: 80, reason: "", candidates: [], siblings: [] });
const base = { id: "i", status: "ready", metadata: meta, proposal: null, target: null, use_cover: true, version: 1 } as unknown as ImportInfo;
const imp = (over: Partial<ImportInfo>): ImportInfo => ({ ...base, ...over });

describe("bulkPlan", () => {
  it("joins only a certain match or a saved choice", () => {
    expect(bulkPlan(imp({ proposal: join("high") }), false)).toEqual({ target: { action: "join", work_id: 7 } });
    expect(bulkPlan(imp({ proposal: join("medium") }), false)).toEqual({ skip: "uncertain" });
    expect(bulkPlan(imp({ proposal: join("low") }), false)).toEqual({ skip: "uncertain" });
    // The person saved where it goes: that is a decision, whatever the proposal's confidence.
    expect(bulkPlan(imp({ proposal: join("medium"), target: { action: "join", work_id: 9 } }), false)).toEqual({ target: { action: "join", work_id: 9 } });
  });

  it("files a new book when nothing matched", () => {
    expect(bulkPlan(imp({}), false)).toEqual({ target: { action: "new" } });
  });

  it("never files over unsaved choices or invalid details", () => {
    expect(bulkPlan(imp({ proposal: join("high") }), true)).toEqual({ skip: "unsaved" });
    expect(bulkPlan(imp({ metadata: { ...meta, title: " " } }), false)).toEqual({ skip: "details" });
    expect(bulkPlan(imp({ metadata: null }), false)).toEqual({ skip: "details" });
  });
});
