import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built SPA is served by webgui/server.py from frontend/dist (it mounts
// /assets and falls through to index.html for client-side routing). In dev,
// `npm run dev` proxies API + websocket calls to the BFF so the browser talks
// to a real controller without CORS gymnastics.
//
// Where the console's own assets are fetched from. RELATIVE by default, so one
// build is correct wherever it is served: at the domain root on a standalone
// Controller, and under /controller/ behind the SLOP gateway, which path-routes
// /controller/* to this app on one shared origin.
//
// It used to default to "/", which bakes an absolute "/assets/index-*.js" into
// index.html — and behind the gateway that asks the SLOP PORTAL for this
// console's script, so the page came up blank with its script, stylesheet and
// API all 404. A fixed prefix could not have fixed it either: the same
// controller is reached BOTH ways, and only one of them would work.
//
// SYSIBLE_BASE_PATH still pins an absolute base for anyone who wants one.
const BASE_PATH = process.env.SYSIBLE_BASE_PATH || "./";

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
