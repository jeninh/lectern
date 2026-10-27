// The Docs tab's renderer (components/DocMarkdown.tsx) on hostile Markdown: nothing a doc
// contains may become live HTML, a script URL, or an image from outside docs/screenshots.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { DocMarkdown } from "../../components/DocMarkdown";
import { parseDoc } from "../docs";

const render = (md: string, files = new Set(["tts.md"])) =>
  renderToStaticMarkup(createElement(MemoryRouter, null, createElement(DocMarkdown, { tokens: parseDoc(md).tokens, files })));

describe("DocMarkdown", () => {
  it("never produces markup, script URLs, or outside images from a doc", () => {
    const html = render(
      [
        "<script>alert(1)</script>",
        "",
        'Hi <img src=x onerror=alert(1)> <a href="javascript:alert(1)">a</a>',
        "",
        "[js](javascript:alert(1)) [vb](vbscript:x) [data](data:text/html,hi) [proto](//evil.example/x) <javascript:alert(1)>",
        "",
        "![remote](https://evil.example/x.png) ![up](../../x.png) ![svg](screenshots/x.svg)",
        "",
        "| a | b |",
        "|---|---|",
        "| <b onclick=x>c</b> | d |",
      ].join("\n"),
    );
    // Every real tag and attribute is from the renderer's allowlist; the hostile parts are text.
    for (const [, tag, attrs] of html.matchAll(/<([a-z0-9]+)([^>]*)>/g)) {
      expect(["p", "span", "div", "table", "thead", "tbody", "tr", "th", "td", "a", "img"], tag).toContain(tag);
      for (const [, name, value] of attrs.matchAll(/\s([a-z-]+)="([^"]*)"/g)) {
        expect(["class", "tabindex", "href", "src", "alt", "loading", "target", "rel"], `${tag} ${name}`).toContain(name);
        if (name === "href") expect(value).toMatch(/^(\/app\/|https:\/\/github\.com\/jeninh\/lectern\/)/);
        if (name === "src") expect(value).toMatch(/^\/app\/doc-media\/[\w.-]+\.png$/);
      }
    }
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;script&gt;");
  });

  it("links docs in the app, other repo files to GitHub, and leaves unknown docs as text", () => {
    const html = render("[t](tts.md#kokoro) [c](../src/lectern/main.py) [hidden](backups.md) ![s](screenshots/a.png)");
    expect(html).toContain('href="/app/docs/tts#kokoro"');
    expect(html).toContain('href="https://github.com/jeninh/lectern/blob/main/src/lectern/main.py"');
    expect(html).not.toContain("backups");
    expect(html).toContain("<span>hidden</span>");
    expect(html).toContain('src="/app/doc-media/a.png"');
  });
});
