import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built SPA is served by webgui/server.py from frontend/dist (it mounts
// /assets and falls through to index.html for client-side routing). In dev,
// `npm run dev` proxies API + websocket calls to the BFF so the browser talks
// to a real controller without CORS gymnastics.
export default defineConfig({
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
