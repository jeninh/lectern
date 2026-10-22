import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { build } from "esbuild";
import { createHash } from "node:crypto";
import { cpSync, readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

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
  plugins: [react(), pdfjsAssets(), serviceWorker()],
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
