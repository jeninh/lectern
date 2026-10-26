// Renders a doc's markdown-it tokens (lib/docs.ts) as React elements. Only the elements and
// attributes listed here are produced; nothing is ever set as HTML, so a doc cannot inject markup
// or script whatever it contains.
import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import type { Token } from "markdown-it";
import { DOC_MEDIA, docPath, resolveLink } from "../lib/docs";
import { Check, Copy } from "./Icons";

const BLOCK_TAGS: Record<string, "p" | "ul" | "ol" | "li" | "table" | "thead" | "tbody" | "tr" | "strong" | "em" | "s"> = {
  paragraph_open: "p",
  bullet_list_open: "ul",
  ordered_list_open: "ol",
  list_item_open: "li",
  table_open: "table",
  thead_open: "thead",
  tbody_open: "tbody",
  tr_open: "tr",
  strong_open: "strong",
  em_open: "em",
  s_open: "s",
};

function CodeBlock({ code, lang }: { code: string; lang: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard refused (insecure context): the text is still selectable */
    }
  };
  if (lang === "diagram") {
    return (
      <figure className="doc-diagram" role="group" aria-label="Diagram">
        <pre>{code}</pre>
      </figure>
    );
  }
  return (
    <div className="doc-code">
      {lang && <span className="doc-code-lang" aria-hidden="true">{lang}</span>}
      <button type="button" className="icon-btn ghost sm doc-copy" onClick={copy} aria-label={copied ? "Copied" : "Copy code"} title={copied ? "Copied" : "Copy"}>
        {copied ? <Check /> : <Copy />}
      </button>
      <pre tabIndex={0}>
        <code>{code}</code>
      </pre>
    </div>
  );
}

/** Index of the token closing the one opened at `i`. */
function closing(tokens: Token[], i: number): number {
  let depth = 0;
  for (let j = i; j < tokens.length; j++) {
    depth += tokens[j].nesting;
    if (depth === 0) return j;
  }
  return tokens.length - 1;
}

export function DocMarkdown({ tokens, files }: { tokens: Token[]; files: ReadonlySet<string> }) {
  const render = (ts: Token[], keyPrefix: string): ReactNode[] => {
    const out: ReactNode[] = [];
    for (let i = 0; i < ts.length; i++) {
      const t = ts[i];
      const key = `${keyPrefix}${i}`;
      if (t.nesting === 1) {
        const end = closing(ts, i);
        const inner = ts.slice(i + 1, end);
        out.push(element(t, inner, key));
        i = end;
      } else out.push(leaf(t, key));
    }
    return out;
  };

  const element = (t: Token, inner: Token[], key: string): ReactNode => {
    const kids = () => render(inner, `${key}.`);
    switch (t.type) {
      case "heading_open": {
        const id = t.attrGet("id") ?? "";
        const Tag = t.tag as "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
        if (Tag === "h1") return null; // the page shows the manifest title instead
        return (
          <Tag key={key} id={id}>
            {kids()}
            <Link className="doc-anchor" to={{ hash: id }} aria-label="Link to this section">#</Link>
          </Tag>
        );
      }
      case "paragraph_open":
        // Tight list items wrap their text in hidden paragraphs.
        return t.hidden ? kids() : <p key={key}>{kids()}</p>;
      case "ordered_list_open": {
        const start = Number(t.attrGet("start") ?? 1);
        return <ol key={key} start={start !== 1 ? start : undefined}>{kids()}</ol>;
      }
      case "blockquote_open": {
        // GitHub's "> [!NOTE]" marks a callout (lib/docs.ts); any other quote is a plain note.
        const kind = t.attrGet("data-callout");
        return (
          <aside key={key} className={`doc-callout ${kind ?? "note"}`}>
            {kind && <strong className="doc-callout-title">{kind[0].toUpperCase() + kind.slice(1)}</strong>}
            {kids()}
          </aside>
        );
      }
      case "th_open":
      case "td_open": {
        const align = /text-align:(left|center|right)/.exec(t.attrGet("style") ?? "")?.[1] as "left" | "center" | "right" | undefined;
        const Tag = t.tag as "th" | "td";
        return <Tag key={key} style={align ? { textAlign: align } : undefined}>{kids()}</Tag>;
      }
      case "table_open":
        return (
          <div key={key} className="doc-table" tabIndex={0}>
            <table>{kids()}</table>
          </div>
        );
      case "link_open":
        return link(t.attrGet("href") ?? "", kids(), key);
      default: {
        const Tag = BLOCK_TAGS[t.type];
        return Tag ? <Tag key={key}>{kids()}</Tag> : <span key={key}>{kids()}</span>;
      }
    }
  };

  const link = (href: string, children: ReactNode, key: string): ReactNode => {
    const to = resolveLink(href, files);
    switch (to.kind) {
      case "doc":
        return <Link key={key} to={docPath(to.slug, to.hash)}>{children}</Link>;
      case "anchor":
        return <Link key={key} to={{ hash: to.hash }}>{children}</Link>;
      case "repo":
      case "external":
        return (
          <a key={key} href={to.kind === "repo" ? to.url : to.href} target="_blank" rel="noreferrer noopener" className="doc-external">
            {children}
          </a>
        );
      case "image":
        return <a key={key} href={`${DOC_MEDIA}/${to.name}`} target="_blank" rel="noreferrer">{children}</a>;
      default:
        return <span key={key}>{children}</span>;
    }
  };

  const leaf = (t: Token, key: string): ReactNode => {
    switch (t.type) {
      case "inline":
        return render(t.children ?? [], `${key}.`);
      case "text":
        return t.content;
      case "softbreak":
        return "\n";
      case "hardbreak":
        return <br key={key} />;
      case "code_inline":
        return <code key={key}>{t.content}</code>;
      case "fence":
        return <CodeBlock key={key} code={t.content.replace(/\n$/, "")} lang={t.info.trim().split(/\s+/)[0]} />;
      case "code_block":
        return <CodeBlock key={key} code={t.content.replace(/\n$/, "")} lang="" />;
      case "hr":
        return <hr key={key} />;
      case "image": {
        const to = resolveLink(t.attrGet("src") ?? "", files);
        const alt = t.children ? t.children.map((c) => c.content).join("") : t.content;
        // Only the docs' own screenshots: the page never loads images from elsewhere.
        if (to.kind !== "image") return alt;
        return <img key={key} className="doc-img" src={`${DOC_MEDIA}/${to.name}`} alt={alt} loading="lazy" />;
      }
      default:
        // html_inline/html_block cannot occur (html is off); anything unknown shows as text.
        return t.content || null;
    }
  };

  return <>{render(tokens, "")}</>;
}
