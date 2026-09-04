import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

import { mockApiPlugin } from "./mock/mock-plugin.mjs";

export default defineConfig({
  // VITE_MOCK=1 时用 mock API 替代后端（UI 开发/视觉验证无需起全栈）；
  // mock 在内部中间件之前挂载，会先于 proxy 命中 /v1/* 与 /health。
  plugins: [react(), ...(process.env.VITE_MOCK === "1" ? [mockApiPlugin()] : [])],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    port: 5173,
    // 开发时代理到后端，避免前端自己处理 CORS 与 API Key 注入
    proxy: {
      "/v1": { target: "http://localhost:8000", changeOrigin: true },
      "/health": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
  build: {
    rollupOptions: {
      output: {
        // 必须用 id 匹配而非模块名数组。列 "echarts/charts" 这类 barrel 入口会让
        // Rollup 把 barrel 的全部导出当作 chunk 入口保留，直接抵消 lib/echarts.ts
        // 的按需注册（实测 echarts chunk 因此膨胀到 1.1MB）。
        //
        // echarts 通过 ChartLazy 动态导入，不放在 manualChunks 里强制打包，让它保持异步。
        manualChunks(id) {
          if (!id.includes("node_modules")) return;
          // 移除 echarts 强制分组，让动态 import 自然分割
          // if (id.includes("echarts") || id.includes("zrender")) return "echarts";
          if (/[\\/]node_modules[\\/](@xyflow|dagre|graphlib)[\\/]/.test(id)) return "graph";
          if (/[\\/]node_modules[\\/]@xterm[\\/]/.test(id)) return "terminal";
          if (
            /[\\/]node_modules[\\/](react|react-dom|scheduler|react-router|react-router-dom|@tanstack)[\\/]/.test(
              id,
            )
          ) {
            return "vendor";
          }
        },
      },
    },
  },
});
