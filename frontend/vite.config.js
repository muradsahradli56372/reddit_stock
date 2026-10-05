import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The browser only talks to /api on the same origin; Vite forwards it to the backend.
// API keys never reach the frontend.
const target = process.env.API_URL || "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: { "/api": { target, changeOrigin: true, rewrite: (p) => p.replace(/^\/api/, "") } },
  },
  preview: {
    host: true,
    port: 5173,
    proxy: { "/api": { target, changeOrigin: true, rewrite: (p) => p.replace(/^\/api/, "") } },
  },
});
