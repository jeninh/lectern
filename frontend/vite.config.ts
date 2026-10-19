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
    proxy: {
      "/api": "http://127.0.0.1:8080",
      "/library": "http://127.0.0.1:8080",
      "/login": "http://127.0.0.1:8080",
      "/logout": "http://127.0.0.1:8080",
      "/static": "http://127.0.0.1:8080",
    },
  },
});
