import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// During `npm run dev`, proxy /api to the BFF on :8800 so the SPA and
// the API share an origin (cookies work, no CORS). In production the BFF
// serves the built dist/ itself, so no proxy is needed.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.SYSIBLE_WEBGUI_API || "http://localhost:8800",
        changeOrigin: true,
        ws: true, // proxy the Sysible Connect terminal websocket too
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
