// The Docs tab's CI check: docs/index.yml lists every docs/*.md, and every link in the docs
// (and every in-app help link) reaches a doc, a heading, a screenshot, or a repository file.
// Also the rendering rules a broken doc could otherwise slip past: anchors, links, raw HTML.
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { parse } from "yaml";
import { AUDIENCES, buildDocs, canSee, docLinks, DOC_ICONS, parseDoc, resolveLink, searchDocs, slugify, type DocLink, type Manifest } from "../docs";
import { HELP, inAppDocUrl } from "../help";

const ROOT = resolve(__dirname, "../../../..");
const DOCS_DIR = resolve(ROOT, "docs");
const manifest = parse(readFileSync(resolve(DOCS_DIR, "index.yml"), "utf8")) as Manifest;
const mdFiles = readdirSync(DOCS_DIR).filter((f) => f.endsWith(".md")).sort();
const sources = Object.fromEntries(mdFiles.map((f) => [f, readFileSync(resolve(DOCS_DIR, f), "utf8")]));
const docs = buildDocs(manifest, sources);
const files = new Set(mdFiles);
const byFile = new Map(docs.map((d) => [d.file, d]));

/** Why a link does not resolve, or null when it does. */
function problem(from: string, to: DocLink, image: boolean): string | null {
  switch (to.kind) {
    case "broken":
      return "does not resolve";
    case "doc":
      return to.hash && !byFile.get(to.file)?.ids.has(to.hash) ? `no heading #${to.hash} in ${to.file}` : null;
    case "anchor":
      return byFile.get(from)?.ids.has(to.hash) ? null : `no heading #${to.hash} on this page`;
    case "image":
      return existsSync(resolve(DOCS_DIR, "screenshots", to.name)) ? null : "screenshot missing";
    case "repo":
      if (image) return "images must be PNGs in docs/screenshots";
      return existsSync(resolve(ROOT, to.path)) ? null : "file not in the repository";
    case "external":
      return image ? "images must be in docs/screenshots (the app never loads remote images)" : null;
  }
}

describe("docs/index.yml", () => {
  it("lists every docs/*.md exactly once, and only files that exist", () => {
    const listed = manifest.sections.flatMap((s) => s.docs.map((d) => d.file));
    expect(listed.filter((f, i) => listed.indexOf(f) !== i), "listed twice").toEqual([]);
    expect(listed.filter((f) => !files.has(f)), "listed but missing from docs/").toEqual([]);
    expect(mdFiles.filter((f) => !listed.includes(f)), "in docs/ but not in docs/index.yml").toEqual([]);
  });

  it("gives every page a title, description, known icon, and known audience", () => {
    for (const s of manifest.sections) {
      expect(s.title, "section title").toBeTruthy();
      for (const d of s.docs) {
        expect(d.title && d.description, d.file).toBeTruthy();
        expect(DOC_ICONS, `${d.file} icon`).toContain(d.icon);
        expect(AUDIENCES, `${d.file} audience`).toContain(d.audience ?? "everyone");
      }
    }
  });
});

describe("links in docs/*.md", () => {
  it("all resolve to a doc, heading, screenshot, repository file, or web page", () => {
    const bad: string[] = [];
    for (const d of docs) {
      for (const l of docLinks(d.tokens, files)) {
        const p = problem(d.file, l.to, l.image);
        if (p) bad.push(`${d.file}: ${l.href} (${p})`);
      }
    }
    expect(bad).toEqual([]);
  });

  it("every in-app help link and GitHub docs URL in the code reaches a heading", () => {
    const bad: string[] = [];
    for (const [topic, target] of Object.entries(HELP)) {
      const p = problem("", resolveLink(target, files), false);
      if (p) bad.push(`HELP.${topic} = ${target} (${p})`);
    }
    const scan = (dir: string, ext: RegExp) => {
      for (const e of readdirSync(dir, { withFileTypes: true, recursive: true })) {
        if (!e.isFile() || !ext.test(e.name) || e.parentPath.includes("node_modules")) continue;
        const text = readFileSync(resolve(e.parentPath, e.name), "utf8");
        for (const [url] of text.matchAll(/https:\/\/github\.com\/jeninh\/lectern\/blob\/[^\s"'`)]+\/docs\/[^\s"'`)]+/g)) {
          const p = problem("", resolveLink(url, files), false);
          if (p) bad.push(`${e.name}: ${url} (${p})`);
        }
      }
    };
    scan(resolve(ROOT, "src/lectern"), /\.py$/);
    scan(resolve(ROOT, "frontend/src"), /\.tsx?$/);
    expect(bad).toEqual([]);
  });
});

describe("rendering rules", () => {
  it("uses GitHub's heading anchors, numbering repeats", () => {
    expect(slugify("4. Updating")).toBe("4-updating");
    expect(slugify("Self-test: `lectern worker-doctor`".replace(/`/g, ""))).toBe("self-test-lectern-worker-doctor");
    expect(slugify("Troubleshooting: Kokoro jobs stuck at 0 % (\"No virtual environment found\")")).toBe("troubleshooting-kokoro-jobs-stuck-at-0--no-virtual-environment-found");
    const { toc } = parseDoc("# T\n## Setup\n### Setup\n## Café — notes\n");
    expect(toc.map((h) => h.id)).toEqual(["setup", "setup-1", "café--notes"]);
  });

  it("keeps raw HTML as text and drops script links", () => {
    const { tokens } = parseDoc('<script>alert(1)</script>\n\nHi <img src=x onerror=alert(1)> [x](javascript:alert(1)) [y](data:text/html,hi)\n');
    const types = (ts: typeof tokens): string[] => ts.flatMap((t) => [t.type, ...types(t.children ?? [])]);
    expect(types(tokens).filter((t) => t.startsWith("html") || t === "link_open")).toEqual([]);
  });

  it("marks GitHub callouts and strips the marker", () => {
    const { tokens } = parseDoc("> [!WARNING]\n> Stop Lectern first.\n");
    expect(tokens[0].attrGet("data-callout")).toBe("warning");
    expect(tokens.find((t) => t.type === "inline")?.content.startsWith("[!")).toBe(true); // source kept
    expect(tokens.find((t) => t.type === "inline")?.children?.[0].content).toBe("Stop Lectern first.");
  });

  it("resolves links the way GitHub does, and keeps them inside the repository", () => {
    const f = new Set(["tts.md", "backups.md"]);
    expect(resolveLink("backups.md#restore", f)).toEqual({ kind: "doc", file: "backups.md", slug: "backups", hash: "restore" });
    expect(resolveLink("./tts.md", f)).toMatchObject({ kind: "doc", slug: "tts", hash: "" });
    expect(resolveLink("../deploy/tailscale/compose.yaml", f)).toEqual({
      kind: "repo",
      path: "deploy/tailscale/compose.yaml",
      hash: "",
      url: "https://github.com/jeninh/lectern/blob/main/deploy/tailscale/compose.yaml",
    });
    expect(resolveLink("../deploy/tailscale/", f)).toMatchObject({ kind: "repo", path: "deploy/tailscale/" });
    expect(resolveLink("screenshots/a-b.png", f)).toEqual({ kind: "image", name: "a-b.png" });
    expect(resolveLink("missing.md", f).kind).toBe("broken");
    expect(resolveLink("../../etc/passwd", f).kind).toBe("broken");
    expect(resolveLink("/app/admin", f).kind).toBe("broken");
    expect(resolveLink("https://github.com/jeninh/lectern/blob/main/docs/tts.md#kokoro", f)).toMatchObject({ kind: "doc", slug: "tts", hash: "kokoro" });
    expect(resolveLink("https://example.com/x.md", f)).toEqual({ kind: "external", href: "https://example.com/x.md" });
  });

  it("turns the server's GitHub docs links into in-app routes", () => {
    expect(inAppDocUrl("https://github.com/jeninh/lectern/blob/main/docs/alignment.md#turning-it-on")).toBe("/app/docs/alignment#turning-it-on");
    expect(inAppDocUrl("https://github.com/jeninh/lectern/blob/main/src/x.py")).toBe("https://github.com/jeninh/lectern/blob/main/src/x.py");
  });
});

describe("audience and search", () => {
  it("hides ops docs from readers and shows owners everything", () => {
    const seen = (role: "owner" | "member" | "reader") => docs.filter((d) => canSee(d.audience, role)).map((d) => d.file);
    expect(seen("owner")).toHaveLength(docs.length);
    expect(seen("reader")).not.toContain("backups.md");
    expect(seen("reader")).not.toContain("pi-setup.md");
    expect(seen("reader")).toContain("offline.md");
    expect(seen("member")).toContain("pi-setup.md");
    expect(seen("member")).not.toContain("backups.md");
  });

  it("finds a heading before passing mentions, and the doc whose title matches", () => {
    const hits = searchDocs(docs, "restore");
    expect(hits[0]).toMatchObject({ heading: "Restore", id: "restore" });
    expect(hits[0].doc.file).toBe("backups.md");
    // Every word must appear: two words that never meet in one section find nothing there.
    expect(searchDocs(docs, "kokoro zzzunlikely")).toEqual([]);
    const intro = searchDocs(docs, "backups").find((h) => !h.id);
    expect(intro?.doc.file).toBe("backups.md");
  });
});
