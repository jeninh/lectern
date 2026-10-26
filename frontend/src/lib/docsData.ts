// The docs themselves, bundled at build time so the Docs tab needs no network and always
// matches this version of the app. Loaded lazily (by the Docs page and the command palette),
// so Markdown and its parser stay out of the main bundle.
import manifest from "../../../docs/index.yml";
import { buildDocs, type Manifest } from "./docs";

export { canSee, docPath, searchDocs } from "./docs";

const sources = import.meta.glob("../../../docs/*.md", { query: "?raw", import: "default", eager: true }) as Record<string, string>;

export const DOCS = buildDocs(
  manifest as Manifest,
  Object.fromEntries(Object.entries(sources).map(([path, text]) => [path.slice(path.lastIndexOf("/") + 1), text])),
);
export const DOC_FILES: ReadonlySet<string> = new Set(DOCS.map((d) => d.file));
