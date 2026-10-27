import { describe, expect, it } from "vitest";
import { articleError, emailInReason, isSavableUrl, parseSenderEntries, sharedUrl } from "../ingest";

describe("sharedUrl (Web Share Target)", () => {
  it("prefers the url field", () => {
    expect(sharedUrl(new URLSearchParams({ url: "https://a.example/x", text: "see https://b.example/y" }))).toBe("https://a.example/x");
  });
  it("finds the link Android puts in text, without trailing punctuation", () => {
    const p = new URLSearchParams({ title: "Great read", text: "Look at this: https://news.example.com/a?id=4&b=2)." });
    expect(sharedUrl(p)).toBe("https://news.example.com/a?id=4&b=2");
  });
  it("is null when nothing shared is a web link", () => {
    expect(sharedUrl(new URLSearchParams({ text: "ftp://x.example/file and mailto:a@b.example" }))).toBeNull();
    expect(sharedUrl(new URLSearchParams())).toBeNull();
  });
});

describe("isSavableUrl", () => {
  it("takes http and https links only, without credentials", () => {
    expect(isSavableUrl(" https://example.com/post ")).toBe(true);
    expect(isSavableUrl("http://example.com")).toBe(true);
    expect(isSavableUrl("javascript:alert(1)")).toBe(false);
    expect(isSavableUrl("file:///etc/passwd")).toBe(false);
    expect(isSavableUrl("https://user:pw@example.com/")).toBe(false);
    expect(isSavableUrl("example.com/post")).toBe(false);
  });
});

describe("parseSenderEntries", () => {
  it("normalises addresses and @domains, drops duplicates, reports the rest", () => {
    const r = parseSenderEntries("Alice@Example.org, @Family.example\n<bob@x.example>; alice@example.org\nnot-an-address @nodot");
    expect(r.entries).toEqual(["alice@example.org", "@family.example", "bob@x.example"]);
    expect(r.invalid).toEqual(["not-an-address", "@nodot"]);
  });
  it("is empty for blank input", () => {
    expect(parseSenderEntries("  \n ,")).toEqual({ entries: [], invalid: [] });
  });
});

describe("messages", () => {
  it("explains article failures and email-in states in words", () => {
    expect(articleError("blocked_address")).toMatch(/private or local address/);
    expect(articleError("something_new")).toBe("something_new");
    expect(articleError(null)).toBe("");
    expect(emailInReason({ reason: null })).toBeNull();
    expect(emailInReason({ reason: "offline" })).toMatch(/offline mode/);
    expect(emailInReason({ reason: "not_configured", missing: ["LECTERN_EMAIL_IN_HOST", "LECTERN_EMAIL_IN_PASSWORD"] })).toBe(
      "Email-in is off: missing or invalid in .env: LECTERN_EMAIL_IN_HOST, LECTERN_EMAIL_IN_PASSWORD.",
    );
    expect(emailInReason({ reason: "not_configured" })).toMatch(/owner can see/);
    expect(emailInReason({ reason: "off" })).toMatch(/not set up/);
  });
});
