import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: `LSXVE_API=https://<host> npm run dev` proxies /api to a running LSX-VE (or the dev API on :8443).
const target = process.env.LSXVE_API || "http://127.0.0.1:8443";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 900,
    rollupOptions: { output: { manualChunks: { vnc: ["@novnc/novnc/lib/rfb"] } } },
  },
  server: {
    port: 5173,
    proxy: { "/api": { target, changeOrigin: false, secure: false, ws: true } },
  },
});
