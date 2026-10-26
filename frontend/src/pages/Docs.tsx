// The Docs tab (/app/docs, /app/docs/<slug>): the repository's docs/*.md grouped by
// docs/index.yml, with search, breadcrumbs, previous/next, and "On this page".
import { Fragment, useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { DocMarkdown } from "../components/DocMarkdown";
import { EmptyState } from "../components/EmptyState";
import { Book, Devices, Download, Gear, Headphones, Home, Key, Search, Send, Shield, Sparkle, Sync, Toc } from "../components/Icons";
import { PageTitle } from "../components/Shell";
import { canSee, docPath, searchDocs, type Doc, type DocIcon } from "../lib/docs";
import { DOC_FILES, DOCS } from "../lib/docsData";
import { fold } from "../lib/fuzzy";
import { useStore } from "../lib/store";

const ICONS: Record<DocIcon, (p: { "aria-hidden"?: boolean }) => JSX.Element> = {
  home: Home,
  shield: Shield,
  devices: Devices,
  download: Download,
  send: Send,
  headphones: Headphones,
  sync: Sync,
  sparkle: Sparkle,
  book: Book,
  key: Key,
  gear: Gear,
  toc: Toc,
};

function DocIconView({ icon }: { icon: DocIcon }) {
  const I = ICONS[icon];
  return <I aria-hidden />;
}

function bySection(docs: Doc[]): [string, Doc[]][] {
  const m = new Map<string, Doc[]>();
  for (const d of docs) m.set(d.section, [...(m.get(d.section) ?? []), d]);
  return [...m.entries()];
}

function highlight(text: string, query: string): ReactNode {
  const words = fold(query).split(/\s+/).filter(Boolean);
  if (!words.length) return text;
  const folded = fold(text);
  // fold() keeps lengths for the accented Latin text the docs use, so offsets line up.
  if (folded.length !== text.length) return text;
  const out: ReactNode[] = [];
  let i = 0;
  while (i < text.length) {
    let best: { at: number; len: number } | null = null;
    for (const w of words) {
      const at = folded.indexOf(w, i);
      if (at >= 0 && (!best || at < best.at)) best = { at, len: w.length };
    }
    if (!best) break;
    out.push(text.slice(i, best.at), <mark key={best.at}>{text.slice(best.at, best.at + best.len)}</mark>);
    i = best.at + best.len;
  }
  out.push(text.slice(i));
  return out;
}

/** The heading currently at the top of the window, for highlighting "On this page". */
function useActiveHeading(ids: string[]): string | null {
  const [active, setActive] = useState<string | null>(null);
  useEffect(() => {
    setActive(null);
    const els = ids.map((id) => document.getElementById(id)).filter((e): e is HTMLElement => !!e);
    if (!els.length || typeof IntersectionObserver === "undefined") return;
    const visible = new Set<string>();
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) (e.isIntersecting ? visible.add(e.target.id) : visible.delete(e.target.id));
        const first = ids.find((id) => visible.has(id));
        if (first) setActive(first);
      },
      { rootMargin: "-64px 0px -70% 0px" },
    );
    els.forEach((e) => io.observe(e));
    return () => io.disconnect();
  }, [ids]);
  return active;
}

export default function DocsPage() {
  const { slug } = useParams();
  const { hash } = useLocation();
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const { me } = useStore();
  const role = me?.user.role;
  const visible = useMemo(() => DOCS.filter((d) => canSee(d.audience, role)), [role]);
  const doc = slug ? DOCS.find((d) => d.slug === slug) ?? null : null;
  const allowed = !!doc && canSee(doc.audience, role);
  const hits = useMemo(() => (q.trim() ? searchDocs(visible, q, 30) : []), [visible, q]);
  const tocIds = useMemo(() => (allowed && doc ? doc.toc.map((h) => h.id) : []), [allowed, doc]);
  const active = useActiveHeading(tocIds);

  // Scroll to the heading in the address; after the Shell's own scroll-to-top on a new page.
  useEffect(() => {
    if (!hash || q) return;
    const id = decodeURIComponent(hash.slice(1));
    const raf = requestAnimationFrame(() => document.getElementById(id)?.scrollIntoView({ block: "start" }));
    return () => cancelAnimationFrame(raf);
  }, [slug, hash, q]);

  const setQuery = (v: string) => setParams(v ? { q: v } : {}, { replace: true });

  const i = doc ? visible.indexOf(doc) : -1;
  const prev = i > 0 ? visible[i - 1] : null;
  const next = i >= 0 && i < visible.length - 1 ? visible[i + 1] : null;

  const toc = allowed && doc && doc.toc.length > 0 && (
    <ul>
      {doc.toc.map((h) => (
        <li key={h.id} data-level={h.level}>
          <Link to={{ hash: h.id }} aria-current={active === h.id ? "location" : undefined}>{h.text}</Link>
        </li>
      ))}
    </ul>
  );

  let body: ReactNode;
  if (q.trim()) {
    body = (
      <section className="docs-results" aria-label="Search results">
        <p className="hint" role="status">{hits.length ? `${hits.length === 30 ? "30+" : hits.length} ${hits.length === 1 ? "result" : "results"} for “${q.trim()}”` : `Nothing in the docs matches “${q.trim()}”.`}</p>
        <ol>
          {hits.map((h) => (
            <li key={`${h.doc.slug}#${h.id}`}>
              <Link to={docPath(h.doc.slug, h.id)}>
                <span className="docs-hit-path">{h.doc.section} › {h.doc.title}</span>
                <strong>{highlight(h.heading || h.doc.title, q)}</strong>
                {h.snippet && <span className="docs-hit-snippet">{highlight(h.snippet, q)}</span>}
              </Link>
            </li>
          ))}
        </ol>
      </section>
    );
  } else if (doc && allowed) {
    body = (
      <article className="doc" aria-label={doc.title}>
        <nav aria-label="Breadcrumb" className="docs-crumbs">
          <ol>
            <li><Link to="/app/docs">Docs</Link></li>
            <li>{doc.section}</li>
            <li aria-current="page">{doc.title}</li>
          </ol>
        </nav>
        {toc && (
          <details className="docs-toc-inline">
            <summary>On this page</summary>
            {toc}
          </details>
        )}
        <div className="doc-body">
          <DocMarkdown tokens={doc.tokens} files={DOC_FILES} />
        </div>
        <nav className="docs-pager" aria-label="Previous and next">
          {prev ? (
            <Link to={docPath(prev.slug)} rel="prev"><span>Previous</span><strong>{prev.title}</strong></Link>
          ) : <span />}
          {next && (
            <Link to={docPath(next.slug)} rel="next" className="next"><span>Next</span><strong>{next.title}</strong></Link>
          )}
        </nav>
      </article>
    );
  } else if (doc || slug) {
    body = (
      <EmptyState
        icon={<Book />}
        title={doc ? "This page is for the people who run Lectern" : "No such page"}
        action={<Link className="btn" to="/app/docs">All docs</Link>}
      >
        {doc ? "Ask the household's owner if you need it." : "It may have moved. Search the docs or browse them all."}
      </EmptyState>
    );
  } else {
    body = (
      <div className="docs-overview">
        {bySection(visible).map(([section, docs]) => (
          <section key={section} aria-labelledby={`ds-${section}`}>
            <h2 id={`ds-${section}`}>{section}</h2>
            <div className="docs-cards">
              {docs.map((d) => (
                <Link key={d.slug} to={docPath(d.slug)} className="docs-card">
                  <span className="docs-card-icon"><DocIconView icon={d.icon} /></span>
                  <span>
                    <strong>{d.title}</strong>
                    <span>{d.description}</span>
                  </span>
                </Link>
              ))}
            </div>
          </section>
        ))}
      </div>
    );
  }

  return (
    <>
      <PageTitle>{doc && allowed && !q ? doc.title : "Docs"}</PageTitle>
      <div className="docs" data-toc={(!q && !!toc) || undefined}>
        <nav className="docs-nav" aria-label="Docs">
          <form
            className="docs-search"
            role="search"
            onSubmit={(e) => {
              e.preventDefault();
              if (hits[0]) nav(docPath(hits[0].doc.slug, hits[0].id));
            }}
          >
            <Search aria-hidden="true" />
            <input
              type="search"
              value={q}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search docs"
              aria-label="Search docs"
              data-page-filter
              spellCheck={false}
              autoComplete="off"
            />
          </form>
          <div className="docs-sections">
            {bySection(visible).map(([section, docs]) => (
              <Fragment key={section}>
                <div className="nav-section">{section}</div>
                {docs.map((d) => (
                  <Link key={d.slug} to={docPath(d.slug)} className="docs-nav-item" aria-current={d === doc && !q ? "page" : undefined}>
                    <DocIconView icon={d.icon} />
                    <span>{d.title}</span>
                  </Link>
                ))}
              </Fragment>
            ))}
          </div>
        </nav>
        <div className="docs-main">{body}</div>
        {!q && toc && (
          <aside className="docs-toc" aria-label="On this page">
            <div className="nav-section">On this page</div>
            {toc}
          </aside>
        )}
      </div>
    </>
  );
}
