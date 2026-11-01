import { describe, expect, it } from "vitest";
import { thumbUrl } from "../../components/Cover";

describe("thumbUrl", () => {
  it("asks for the thumbnail of a work's cover, keeping the cache-busting version", () => {
    expect(thumbUrl("/library/works/12/cover.jpg?v=4")).toBe("/library/works/12/cover.jpg?v=4&w=360");
    expect(thumbUrl("/library/works/12/cover.jpg")).toBe("/library/works/12/cover.jpg?w=360");
  });
  it("leaves every other image alone", () => {
    expect(thumbUrl("/api/imports/3/cover")).toBe("/api/imports/3/cover");
    expect(thumbUrl("/library/works/12/cover.jpg.bak")).toBe("/library/works/12/cover.jpg.bak");
  });
});
