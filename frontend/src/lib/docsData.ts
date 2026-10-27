// The docs themselves. Docs for everyone are bundled at build time (vite.config.ts), so they
// need no network and match this version of the app. Docs for members or the owner never go
// into the bundle, which every browser receives: members and owners fetch them from the server
// (routes/spa.py), which sends only what their role may see. Loaded lazily (by the Docs page
// and the command palette), so Markdown and its parser stay out of the main bundle.
import { useEffect, useState } from "react";
import publicText from "virtual:lectern-docs";
import { get } from "./api";
import { docsFromText, type Doc, type DocText } from "./docs";
import type { Role } from "./types";

export { canSee, docPath, searchDocs } from "./docs";

/** The docs for everyone, bundled. */
export const PUBLIC_DOCS = docsFromText(publicText);

const loaded = new Map<Role, Promise<Doc[]>>();

/** The docs this role may read: the bundled ones for readers, and when the server cannot be
 * reached (offline); the server's for members and owners. */
export function loadDocs(role: Role | undefined): Promise<Doc[]> {
  if (role !== "owner" && role !== "member") return Promise.resolve(PUBLIC_DOCS);
  let docs = loaded.get(role);
  if (!docs) {
    docs = get<DocText>("/app/doc-text")
      .then(docsFromText)
      .catch(() => {
        loaded.delete(role);
        return PUBLIC_DOCS;
      });
    loaded.set(role, docs);
  }
  return docs;
}

/** loadDocs as state: null until this role's docs are known. */
export function useDocs(role: Role | undefined): Doc[] | null {
  const [state, setState] = useState<{ role: Role | undefined; docs: Doc[] } | null>(null);
  useEffect(() => {
    let live = true;
    loadDocs(role).then((docs) => live && setState({ role, docs }));
    return () => {
      live = false;
    };
  }, [role]);
  return state && state.role === role ? state.docs : null;
}
