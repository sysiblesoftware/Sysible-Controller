import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built SPA is served by webgui/server.py from frontend/dist (it mounts
// /assets and falls through to index.html for client-side routing). In dev,
// `npm run dev` proxies API + websocket calls to the BFF so the browser talks
// to a real controller without CORS gymnastics.
//
// SYSIBLE_BASE_PATH is the URL prefix the console is served under. It is "/" for a
// standalone Controller (served at the domain root) and "/controller/" when it runs
// behind the SLOP gateway, which path-routes /controller/* to this app on one shared
// origin. Vite rewrites every asset URL in index.html to this base, and the SPA reads
// it back via import.meta.env.BASE_URL to prefix its API calls (see src/api.js).
const BASE_PATH = process.env.SYSIBLE_BASE_PATH || "/";

export default defineConfig({
  base: BASE_PATH,
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.SYSIBLE_WEBGUI_DEV_TARGET || "http://127.0.0.1:8800",
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
