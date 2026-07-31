import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The FastAPI development server. Only `/dashboard/api` is proxied, so the Vite
// dev server keeps serving the application itself while every API call reaches
// the real backend and its real session cookie.
const BACKEND = "http://127.0.0.1:8000";

export default defineConfig({
  // FastAPI serves the compiled application under /dashboard, so every asset
  // URL Vite emits has to be rooted there too.
  base: "/dashboard/",
  plugins: [react()],
  build: {
    outDir: "dist",
    assetsDir: "assets",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/dashboard/api": {
        target: BACKEND,
        changeOrigin: false,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: true,
    restoreMocks: true,
  },
});
