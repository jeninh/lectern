import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { build } from "esbuild";
import { createHash } from "node:crypto";
import { cpSync, existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { parse as parseYaml } from "yaml";
import { docLinks, parseDoc, type Manifest } from "./src/lib/docs";

const OUT = resolve(__dirname, "../src/lectern/static/app");

// pdf.js loads character maps, standard fonts, and image decoders at runtime by URL; ship them
// next to the app (same origin, so the CSP stays 'self').
const pdfjsAssets = () => ({
  name: "lectern-pdfjs-assets",
  apply: "build" as const,
  closeBundle() {
    for (const dir of ["cmaps", "standard_fonts", "wasm"]) {
      cpSync(resolve(__dirname, "node_modules/pdfjs-dist", dir), resolve(OUT, "pdfjs", dir), { recursive: true });
    }
  },
});

// The screenshots the docs show (only those; docs/screenshots also holds PR captures), outside
// assets/ so the service worker does not precache megabytes of images. routes/spa.py serves
// them to signed-in users at /app/doc-media/.
const docMedia = () => ({
  name: "lectern-doc-media",
  apply: "build" as const,
  closeBundle() {
    const docs = resolve(__dirname, "../docs");
    mkdirSync(resolve(OUT, "doc-media"), { recursive: true });
    for (const f of readdirSync(docs).filter((n) => n.endsWith(".md"))) {
      for (const l of docLinks(parseDoc(readFileSync(resolve(docs, f), "utf8")).tokens, new Set())) {
        if (l.to.kind === "image") cpSync(resolve(docs, "screenshots", l.to.name), resolve(OUT, "doc-media", l.to.name));
      }
    }
  },
});

// The docs' text, split by audience (docs/index.yml). Only "everyone" docs go into the bundle
// (virtual:lectern-docs): /app/assets is public and precached, so anything there reaches every
// browser. Every doc goes into doc-text.json, outside assets/, which routes/spa.py serves at
// /app/doc-text filtered by the signed-in person's role. The dev server answers that route
// itself with every doc.
const DOCS_DIR = resolve(__dirname, "../docs");
const VIRTUAL_DOCS = "virtual:lectern-docs";
function docText(publicOnly: boolean) {
  const manifest = parseYaml(readFileSync(resolve(DOCS_DIR, "index.yml"), "utf8")) as Manifest;
  return {
    sections: manifest.sections
      .map((s) => ({
        title: s.title,
        docs: s.docs
          .filter((d) => !publicOnly || (d.audience ?? "everyone") === "everyone")
          .filter((d) => existsSync(resolve(DOCS_DIR, d.file)))
          .map((d) => ({ ...d, source: readFileSync(resolve(DOCS_DIR, d.file), "utf8") })),
      }))
      .filter((s) => s.docs.length),
  };
}
const docsText = () => ({
  name: "lectern-docs-text",
  resolveId(id: string) {
    return id === VIRTUAL_DOCS ? `\0${VIRTUAL_DOCS}` : undefined;
  },
  load(this: { addWatchFile(f: string): void }, id: string) {
    if (id !== `\0${VIRTUAL_DOCS}`) return undefined;
    this.addWatchFile(resolve(DOCS_DIR, "index.yml"));
    for (const f of readdirSync(DOCS_DIR).filter((n) => n.endsWith(".md"))) this.addWatchFile(resolve(DOCS_DIR, f));
    return `export default ${JSON.stringify(docText(true))};`;
  },
  configureServer(server: { middlewares: { use(path: string, fn: (req: unknown, res: { setHeader(k: string, v: string): void; end(b: string): void }) => void): void } }) {
    server.middlewares.use("/app/doc-text", (_req, res) => {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(docText(false)));
    });
  },
  closeBundle() {
    writeFileSync(resolve(OUT, "doc-text.json"), JSON.stringify(docText(false)));
  },
});

// The service worker (sw/sw.ts), built after the app so it can precache this build's files.
// Its version is a hash of those files and its own source: any change to the app is a new
// worker, which the app offers as "Update ready" (docs/offline.md).
const serviceWorker = () => ({
  name: "lectern-service-worker",
  apply: "build" as const,
  async closeBundle() {
    const assets = readdirSync(resolve(OUT, "assets")).sort();
    const precache = [
      ...assets.map((f) => `/app/assets/${f}`),
      "/app/manifest.webmanifest",
      "/app/pwa/icon.svg",
      "/app/pwa/icon-192.png",
      "/app/pwa/apple-touch-icon.png",
    ];
    const hash = createHash("sha256");
    for (const f of assets) hash.update(f).update(readFileSync(resolve(OUT, "assets", f)));
    hash.update(readFileSync(resolve(__dirname, "sw/sw.ts"))).update(readFileSync(resolve(__dirname, "src/lib/offline/routes.ts")));
    hash.update(readFileSync(resolve(OUT, "index.html"))).update(readFileSync(resolve(OUT, "manifest.webmanifest")));
    const version = hash.digest("hex").slice(0, 12);
    await build({
      entryPoints: [resolve(__dirname, "sw/sw.ts")],
      bundle: true,
      format: "iife",
      target: "es2020",
      minify: true,
      outfile: resolve(OUT, "sw.js"),
      define: { __LECTERN_VERSION__: JSON.stringify(version), __LECTERN_PRECACHE__: JSON.stringify(precache) },
      logLevel: "warning",
    });
  },
});

// Build output is served by FastAPI from src/lectern/static/app at /app.
export default defineConfig({
  plugins: [react(), docsText(), pdfjsAssets(), docMedia(), serviceWorker()],
  base: "/app/",
  build: {
    outDir: OUT,
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    // Keep the browser's Host (changeOrigin: false) so the backend's same-origin CSRF check
    // accepts writes from :5173. That only applies with LECTERN_INSECURE_HTTP=1.
    proxy: Object.fromEntries(
      ["/api", "/library", "/login", "/logout", "/setup", "/static"].map((p) => [p, { target: "http://127.0.0.1:8080", changeOrigin: false }]),
    ),
  },
});
