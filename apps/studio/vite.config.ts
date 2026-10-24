import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: { "/v1": "http://127.0.0.1:8765", "/health": "http://127.0.0.1:8765", "/ready": "http://127.0.0.1:8765" },
  },
  test: { environment: "jsdom", testTimeout: 15_000, include: ["src/**/*.test.{ts,tsx}"] },
});
