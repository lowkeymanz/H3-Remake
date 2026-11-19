import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// 开发代理：后端 uvicorn 缺省 127.0.0.1:8000（5.4 联调入口）；/ws 走 WS 代理。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/files": "http://127.0.0.1:8000",
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});