import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output is served by FastAPI from src/lectern/static/app at /app.
export default defineConfig({
  plugins: [react()],
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
