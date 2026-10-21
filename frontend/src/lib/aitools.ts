// AI tools (MCP): key scopes and the exact config each AI app needs (docs/mcp.md).

export type AIScope = "upload" | "read" | "organize";

export interface AIKey {
  id: number;
  label: string;
  prefix: string;
  scopes: AIScope[];
  created_at: string;
  last_used_at: string | null;
  last_ip: string | null;
  last_client: string | null;
  revoked_at: string | null;
}

export interface AIKeyList {
  keys: AIKey[];
  scopes: AIScope[];
  default_scopes: AIScope[];
  endpoint: string;
}

export interface CreatedAIKey {
  key: AIKey;
  secret: string;
  endpoint: string;
}

export const SCOPE_TEXT: Record<AIScope, { label: string; detail: string }> = {
  upload: { label: "Add books", detail: "Upload books and audiobooks and file them after review." },
  read: { label: "Read the library", detail: "Search, book details, alignment status, and your progress." },
  organize: { label: "Organize", detail: "Shelves, send to device, start alignment, and edit metadata." },
};

export type AIApp = "claude-desktop" | "claude-code";

/** The server name the AI app shows; a fixed, shell- and JSON-safe identifier. */
const NAME = "lectern";

/**
 * Claude Desktop starts local (stdio) servers from its config file, so it reaches a remote
 * Streamable HTTP server with a custom header through the ``mcp-remote`` bridge (needs
 * Node.js). The header value lives in ``env`` because some platforms split arguments on spaces.
 */
export function claudeDesktopConfig(endpoint: string, secret: string): string {
  return JSON.stringify(
    {
      mcpServers: {
        [NAME]: {
          command: "npx",
          args: ["-y", "mcp-remote", endpoint, "--header", "Authorization:${LECTERN_AUTH}"],
          env: { LECTERN_AUTH: `Bearer ${secret}` },
        },
      },
    },
    null,
    2,
  );
}

/** Claude Code speaks Streamable HTTP natively. */
export function claudeCodeCommand(endpoint: string, secret: string): string {
  return `claude mcp add --transport http ${NAME} ${endpoint} --header "Authorization: Bearer ${secret}"`;
}

/** The same server for a project's ``.mcp.json`` (Claude Code). */
export function claudeCodeJson(endpoint: string, secret: string): string {
  return JSON.stringify(
    { mcpServers: { [NAME]: { type: "http", url: endpoint, headers: { Authorization: `Bearer ${secret}` } } } },
    null,
    2,
  );
}

