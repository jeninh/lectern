// More ways to add books (issue #67): saving a web article as an EPUB, and email-in. The
// server does the work (docs/save-articles.md, docs/email-in.md); this is the API and the
// small pure helpers the screens share.

import { del, get, post, put } from "./api";

export interface ArticleJob {
  id: number;
  url: string | null;
  status: "queued" | "leased" | "succeeded" | "failed" | "cancelled";
  error: string | null;
  title: string | null;
  work_id: number | null;
  images: number | null;
  skipped_images: number | null;
  created_at: string | null;
  finished_at: string | null;
}

export interface ArticlesState {
  enabled: boolean;
  reason: "offline" | null;
  jobs: ArticleJob[];
}

export const articlesState = () => get<ArticlesState>("/api/articles");
export const saveArticle = (url: string) => post<ArticleJob>("/api/articles", { url });

export interface EmailInState {
  enabled: boolean;
  reason: "off" | "not_configured" | "offline" | null;
  address: string | null;
  has_address: boolean;
  shelf: { id: number; name: string } | null;
  shelf_missing: boolean;
  formats: string[];
  max_mb: number | null;
  poll_minutes: number | null;
  filter: { mode: "allowlist" | "blocklist"; source: "env" | "app"; count: number; entries?: string[] };
  last_poll: { at: string | null; ok: boolean | null; error?: string | null };
  /** Owner only: the .env keys that are missing or invalid (names, never values). */
  missing?: string[];
}

export const emailInState = () => get<EmailInState>("/api/email-in");
export const newEmailAddress = () => post<EmailInState>("/api/email-in/address");
export const removeEmailAddress = () => del<void>("/api/email-in/address");
export const setEmailShelf = (shelfId: number | null) => put<EmailInState>("/api/email-in/shelf", { shelf_id: shelfId });
export const setSenderFilter = (mode: "allowlist" | "blocklist", entries: string[], acknowledgeOpen = false) =>
  put<EmailInState>("/api/email-in/senders", { mode, entries, acknowledge_open: acknowledgeOpen });

const URL_IN_TEXT = /https?:\/\/[^\s<>"']+/i;

/**
 * The link to save from what the browser's share sheet sent (Web Share Target): the `url`
 * field when it holds one, else the first http(s) link in `text` or `title` (Android often
 * puts the link in `text`). Trailing punctuation that is not part of a link is dropped.
 */
export function sharedUrl(params: URLSearchParams): string | null {
  for (const key of ["url", "text", "title"]) {
    const value = params.get(key);
    if (!value) continue;
    const m = value.match(URL_IN_TEXT);
    if (m) return m[0].replace(/[).,;:!?]+$/, "");
  }
  return null;
}

/** Whether the server would take this as a link to save (http/https, a host, no password). */
export function isSavableUrl(raw: string): boolean {
  try {
    const u = new URL(raw.trim());
    return (u.protocol === "http:" || u.protocol === "https:") && !!u.hostname && !u.username && !u.password;
  } catch {
    return false;
  }
}

const ARTICLE_ERRORS: Record<string, string> = {
  offline: "Offline mode is on, so Lectern cannot fetch web pages.",
  bad_url: "That is not a web link Lectern can save (http or https only).",
  blocked_address: "That link points at a private or local address; Lectern only saves pages from the public internet.",
  unreachable: "The site could not be reached.",
  timeout: "The site took too long to answer.",
  tls_error: "The site's secure connection could not be verified.",
  http_error: "The site refused the request.",
  too_large: "The page is too large to save.",
  too_many_redirects: "The link redirects too many times.",
  unsupported_encoding: "The site sent the page in a form Lectern cannot read.",
  not_html: "That link is not a web page (it may be a file: download it and upload it instead).",
  no_article: "No readable article on that page (it may need JavaScript or a login).",
  already_saved: "You already saved this article.",
  not_allowed: "Your account can no longer add books.",
  build_failed: "The article could not be turned into a book.",
};

/** Plain words for an article save's error code. */
export function articleError(code: string | null): string {
  if (!code) return "";
  return ARTICLE_ERRORS[code] ?? code;
}

export const articleActive = (j: ArticleJob) => j.status === "queued" || j.status === "leased";

/**
 * Sender filter entries typed one per line or comma-separated: each an address or an
 * `@domain`, lowercased and without duplicates (as the server stores them). `invalid` lists
 * what is neither, so the form can say which.
 */
export function parseSenderEntries(text: string): { entries: string[]; invalid: string[] } {
  const entries: string[] = [];
  const invalid: string[] = [];
  const atom = "[^@\\s\"<>()\\[\\],;:\\\\]+";
  const address = new RegExp(`^${atom}@${atom}\\.${atom}$`);
  const domain = new RegExp(`^@${atom}\\.${atom}$`);
  for (const raw of text.split(/[\s,;]+/)) {
    const v = raw.trim().replace(/^<|>$/g, "").toLowerCase();
    if (!v) continue;
    if (!address.test(v) && !domain.test(v)) invalid.push(raw.trim());
    else if (!entries.includes(v)) entries.push(v);
  }
  return { entries, invalid };
}

/** Why email-in is not working right now, in plain words (null when it is). */
export function emailInReason(s: Pick<EmailInState, "reason" | "missing">): string | null {
  switch (s.reason) {
    case null:
      return null;
    case "offline":
      return "Email-in is paused while offline mode is on. Mail waits in the mailbox until it is off.";
    case "not_configured":
      return s.missing?.length
        ? `Email-in is off: missing or invalid in .env: ${s.missing.join(", ")}.`
        : "Email-in is not set up correctly on this server. The owner can see what is missing in Admin.";
    default:
      return "Email-in is not set up on this server. The owner turns it on in .env (Docs → Email books in).";
  }
}
