import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { cpSync } from "node:fs";
import { resolve } from "node:path";

// pdf.js loads character maps, standard fonts, and image decoders at runtime by URL; ship them
// next to the app (same origin, so the CSP stays 'self').
const pdfjsAssets = () => ({
  name: "lectern-pdfjs-assets",
  apply: "build" as const,
  closeBundle() {
    for (const dir of ["cmaps", "standard_fonts", "wasm"]) {
      cpSync(resolve(__dirname, "node_modules/pdfjs-dist", dir), resolve(__dirname, "../src/lectern/static/app/pdfjs", dir), { recursive: true });
    }
  },
});

// Build output is served by FastAPI from src/lectern/static/app at /app.
export default defineConfig({
  plugins: [react(), pdfjsAssets()],
  base: "/app/",
  build: {
    outDir: "../src/lectern/static/app",
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
