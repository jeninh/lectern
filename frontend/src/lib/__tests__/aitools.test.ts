import { describe, expect, it } from "vitest";
import { claudeCodeCommand, claudeCodeJson, claudeDesktopConfig } from "../aitools";

const URL = "https://lectern.tail1234.ts.net/mcp";
const KEY = "lak_abc-DEF_123";

describe("AI app configuration", () => {
  it("Claude Desktop bridges to the URL with the bearer header kept out of argv", () => {
    const cfg = JSON.parse(claudeDesktopConfig(URL, KEY));
    const server = cfg.mcpServers.lectern;
    expect(server.command).toBe("npx");
    expect(server.args).toEqual(["-y", "mcp-remote", URL, "--header", "Authorization:${LECTERN_AUTH}"]);
    expect(server.env).toEqual({ LECTERN_AUTH: `Bearer ${KEY}` });
  });
  it("Claude Code uses Streamable HTTP with the Authorization header", () => {
    expect(claudeCodeCommand(URL, KEY)).toBe(`claude mcp add --transport http lectern ${URL} --header "Authorization: Bearer ${KEY}"`);
    expect(JSON.parse(claudeCodeJson(URL, KEY))).toEqual({
      mcpServers: { lectern: { type: "http", url: URL, headers: { Authorization: `Bearer ${KEY}` } } },
    });
  });
});
