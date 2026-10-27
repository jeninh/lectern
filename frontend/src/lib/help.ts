// Links from in-app hints and errors to the Docs tab (pages/Docs.tsx). Kept free of the docs
// themselves so the main bundle stays small; __tests__/docs.test.ts checks that every entry
// here, and every GitHub docs URL in the code, resolves to a doc and heading.

/** Where links to code and other repository files point. */
export const REPO_URL = "https://github.com/jeninh/lectern";
/** A doc on GitHub: group 1 is its repository path (docs/…), group 2 the #anchor. */
export const DOCS_ON_GITHUB = /^https:\/\/github\.com\/jeninh\/lectern\/blob\/[^/]+\/(docs\/[^#?]+)(#.*)?$/;

export function docPath(slug: string, hash = ""): string {
  return `/app/docs/${slug}${hash ? `#${hash}` : ""}`;
}

/** Doc sections that in-app hints point to, as `file.md#heading-id`. */
export const HELP = {
  voiceSamples: "tts.md#voice-samples",
  kokoro: "tts.md#kokoro",
  restore: "backups.md#restore",
  workerProxy: "mac-worker.md#school-and-proxy-mode",
  aiConnect: "mcp.md#connect-an-app",
} as const;
export type HelpTopic = keyof typeof HELP;

export function helpPath(topic: HelpTopic): string {
  const [file, hash] = HELP[topic].split("#");
  return docPath(file.replace(/\.md$/, ""), hash);
}

/** The in-app route for a GitHub link to one of the docs (the server's hints use those, e.g.
 * "how to turn on alignment"), or the URL unchanged. */
export function inAppDocUrl(url: string): string {
  const m = DOCS_ON_GITHUB.exec(url);
  const file = m && /^docs\/([^/]+)\.md$/.exec(m[1]);
  return file ? docPath(file[1], decodeURIComponent((m[2] ?? "").slice(1))) : url;
}
