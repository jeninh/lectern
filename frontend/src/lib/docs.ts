// The in-app Docs tab: the repository's docs/*.md, listed and grouped by docs/index.yml, bundled
// at build time (lib/docsData.ts). This module is pure (no React, no DOM, no bundler features) so
// the CI check in __tests__/docs.test.ts validates exactly what the app renders.
//
// Markdown is parsed with raw HTML off: `<script>` or `<img onerror>` in a doc arrives as plain
// text, and markdown-it drops javascript:, vbscript:, file:, and data: links. The tokens are
// turned into React elements from an allowlist (components/DocMarkdown.tsx), never into HTML.

import MarkdownIt, { type Token } from "markdown-it";
import { fold } from "./fuzzy";
import { DOCS_ON_GITHUB, REPO_URL } from "./help";
import type { Role } from "./types";

export { docPath } from "./help";
/** Screenshots referenced by the docs, copied into the build by vite.config.ts and served
 * to signed-in users by routes/spa.py. */
export const DOC_MEDIA = "/app/doc-media";

export const DOC_ICONS = ["home", "shield", "devices", "download", "send", "headphones", "sync", "sparkle", "book", "key", "gear", "toc"] as const;
export type DocIcon = (typeof DOC_ICONS)[number];
export const AUDIENCES = ["everyone", "members", "owner"] as const;
export type Audience = (typeof AUDIENCES)[number];

/** docs/index.yml as written. */
export interface Manifest {
  sections: { title: string; docs: { file: string; title: string; description: string; icon: string; audience?: string }[] }[];
}

export interface Heading {
  level: number;
  text: string;
  id: string;
}

/** A stretch of a doc for search: the introduction, or one h2/h3 and the text under it. */
export interface DocPart {
  heading: string;
  id: string;
  text: string;
}

export interface Doc {
  file: string;
  slug: string;
  title: string;
  description: string;
  icon: DocIcon;
  audience: Audience;
  section: string;
  tokens: Token[];
  /** h2 and h3, for "On this page". */
  toc: Heading[];
  /** Every heading id, for anchors. */
  ids: Set<string>;
  parts: DocPart[];
}

const CALLOUT = /^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*/i;
const md = new MarkdownIt({ html: false, linkify: false, typographer: false });

/** GitHub's heading anchors, so `file.md#4-updating` works both on GitHub and in the app. */
export function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\p{L}\p{M}\p{N}\p{Pc}\- ]/gu, "")
    .replace(/ /g, "-");
}

/** The text of an inline token's children, as GitHub uses it for anchors and as search sees it. */
export function inlineText(children: Token[] | null): string {
  let s = "";
  for (const c of children ?? []) {
    if (c.type === "text" || c.type === "code_inline") s += c.content;
    else if (c.type === "softbreak" || c.type === "hardbreak") s += " ";
    else if (c.type === "image") s += inlineText(c.children);
  }
  return s;
}

/** Parse one doc: tokens with heading ids set, the table of contents, and search parts. */
export function parseDoc(source: string): { tokens: Token[]; toc: Heading[]; ids: Set<string>; parts: DocPart[]; h1: string } {
  const tokens = md.parse(source, {});
  const ids = new Set<string>();
  const seen = new Map<string, number>();
  const toc: Heading[] = [];
  const parts: DocPart[] = [{ heading: "", id: "", text: "" }];
  let h1 = "";
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if (t.type === "heading_open") {
      const text = inlineText(tokens[i + 1].children).trim();
      const base = slugify(text);
      const n = seen.get(base) ?? 0;
      seen.set(base, n + 1);
      const id = n ? `${base}-${n}` : base;
      t.attrSet("id", id);
      ids.add(id);
      const level = Number(t.tag.slice(1));
      if (level === 1 && !h1) h1 = text;
      if (level === 2 || level === 3) {
        toc.push({ level, text, id });
        parts.push({ heading: text, id, text: "" });
      }
      i += 2;
    } else if (t.type === "blockquote_open" && tokens[i + 2]?.type === "inline") {
      const head = tokens[i + 2].children?.[0];
      const m = head?.type === "text" ? CALLOUT.exec(head.content) : null;
      if (head && m) {
        t.attrSet("data-callout", m[1].toLowerCase());
        head.content = head.content.slice(m[0].length);
        const kids = tokens[i + 2].children!;
        if (!head.content && kids[1]?.type === "softbreak") kids.splice(0, 2);
      }
    } else if (t.type === "inline") {
      parts[parts.length - 1].text += `${inlineText(t.children)} `;
    } else if ((t.type === "fence" && t.info.trim() !== "diagram") || t.type === "code_block") {
      parts[parts.length - 1].text += `${t.content} `;
    }
  }
  return { tokens, toc, ids, parts, h1 };
}

/** The manifest and Markdown sources as docs, in manifest order. Entries without a source
 * are skipped here; the CI check reports them. */
export function buildDocs(manifest: Manifest, sources: Record<string, string>): Doc[] {
  const docs: Doc[] = [];
  for (const section of manifest.sections) {
    for (const d of section.docs) {
      const source = sources[d.file];
      if (source == null) continue;
      const { tokens, toc, ids, parts } = parseDoc(source);
      docs.push({
        file: d.file,
        slug: d.file.replace(/\.md$/, ""),
        title: d.title,
        description: d.description,
        icon: (DOC_ICONS as readonly string[]).includes(d.icon) ? (d.icon as DocIcon) : "book",
        audience: (AUDIENCES as readonly string[]).includes(d.audience ?? "") ? (d.audience as Audience) : "everyone",
        section: section.title,
        tokens,
        toc,
        ids,
        parts,
      });
    }
  }
  return docs;
}

/** Ops and admin docs are hidden from readers; owners see everything. */
export function canSee(audience: Audience, role: Role | undefined): boolean {
  if (audience === "everyone") return true;
  if (audience === "members") return role === "owner" || role === "member";
  return role === "owner";
}

export type DocLink =
  /** Another doc (or this one by file name), optionally at a heading. */
  | { kind: "doc"; file: string; slug: string; hash: string }
  /** A heading on the same page. */
  | { kind: "anchor"; hash: string }
  /** A screenshot in docs/screenshots/. */
  | { kind: "image"; name: string }
  /** Any other file in the repository (code, compose files, folders). */
  | { kind: "repo"; path: string; hash: string; url: string }
  | { kind: "external"; href: string }
  /** A relative link that leaves the repository, a .md that is not a doc, or an unknown scheme. */
  | { kind: "broken"; href: string };

function decode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

/** Where a link in docs/<file>.md goes. `files` are the docs' file names ("tts.md"). */
export function resolveLink(href: string, files: ReadonlySet<string>): DocLink {
  const gh = DOCS_ON_GITHUB.exec(href);
  if (gh) href = `../${gh[1]}${gh[2] ?? ""}`;
  if (/^(https?:|mailto:)/i.test(href)) return { kind: "external", href };
  if (href.startsWith("#")) return { kind: "anchor", hash: decode(href.slice(1)) };
  if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith("/")) return { kind: "broken", href };
  const [rawPath, ...rest] = href.split("#");
  const hash = decode(rest.join("#"));
  // Resolve against docs/, then refuse anything that climbs out of the repository.
  const out: string[] = ["docs"];
  for (const seg of decode(rawPath.split("?")[0]).split("/")) {
    if (seg === "" || seg === ".") continue;
    if (seg === "..") {
      if (!out.length) return { kind: "broken", href };
      out.pop();
    } else out.push(seg);
  }
  const path = out.join("/");
  const trailing = rawPath.endsWith("/") ? "/" : "";
  const inDocs = /^docs\/([^/]+\.md)$/.exec(path);
  if (inDocs) {
    return files.has(inDocs[1]) ? { kind: "doc", file: inDocs[1], slug: inDocs[1].replace(/\.md$/, ""), hash } : { kind: "broken", href };
  }
  const shot = /^docs\/screenshots\/([\w.-]+\.png)$/.exec(path);
  if (shot) return { kind: "image", name: shot[1] };
  if (!path) return { kind: "broken", href };
  return { kind: "repo", path: path + trailing, hash, url: `${REPO_URL}/blob/main/${path}${hash ? `#${hash}` : ""}` };
}

/** Every link and image in a doc's tokens, with where it points. */
export function docLinks(tokens: Token[], files: ReadonlySet<string>): { href: string; image: boolean; to: DocLink }[] {
  const out: { href: string; image: boolean; to: DocLink }[] = [];
  const walk = (ts: Token[]) => {
    for (const t of ts) {
      if (t.type === "link_open" || t.type === "image") {
        const href = t.attrGet(t.type === "image" ? "src" : "href") ?? "";
        out.push({ href, image: t.type === "image", to: resolveLink(href, files) });
      }
      if (t.children) walk(t.children);
    }
  };
  walk(tokens);
  return out;
}

export interface SearchHit {
  doc: Doc;
  heading: string;
  id: string;
  snippet: string;
  score: number;
}

/** Full-text search over the given docs: every word must appear in the doc's title or
 * description, a heading, or the text under it. Heading and title matches rank first. */
export function searchDocs(docs: Doc[], query: string, limit = 20): SearchHit[] {
  const words = fold(query).split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  const hits: SearchHit[] = [];
  for (const doc of docs) {
    const title = fold(doc.title);
    const about = fold(doc.description);
    for (const part of doc.parts) {
      const heading = fold(part.heading);
      const text = fold(part.text);
      let score = 0;
      let ok = true;
      for (const w of words) {
        const inHeading = heading.includes(w);
        const inTitle = title.includes(w);
        const inAbout = about.includes(w);
        const inText = text.includes(w);
        if (!inHeading && !inTitle && !inAbout && !inText) {
          ok = false;
          break;
        }
        score += (inHeading ? 10 : 0) + (inTitle ? 8 : inAbout ? 3 : 0) + (inText ? 1 : 0);
      }
      if (!ok) continue;
      // A doc whose title matches is found once, at its top, rather than once per section.
      if (part.id && !words.some((w) => heading.includes(w) || text.includes(w))) continue;
      hits.push({ doc, heading: part.heading, id: part.id, snippet: snippet(part.text, words), score: score + (part.id ? 0 : 1) });
    }
  }
  return hits.sort((a, b) => b.score - a.score).slice(0, limit);
}

function snippet(text: string, words: string[]): string {
  const clean = text.replace(/\s+/g, " ").trim();
  const folded = fold(clean);
  const at = Math.max(0, Math.min(...words.map((w) => folded.indexOf(w)).filter((i) => i >= 0), clean.length));
  const start = at < 60 ? 0 : clean.lastIndexOf(" ", at - 50) + 1;
  const s = clean.slice(start, start + 160);
  return `${start > 0 ? "…" : ""}${s}${start + 160 < clean.length ? "…" : ""}`;
}
