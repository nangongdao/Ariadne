import { defineConfig } from "@playwright/test";

/**
 * E2E 配置：用本机 Chrome 驱动（D:\Tools\Chrome-bin\chrome.exe），不下载 Chromium。
 * webServer 起 VITE_MOCK=1 的 dev server —— mock API 中间件覆盖 /v1/*，
 * 无需后端全栈即可跑通渲染/守卫/响应式流程。
 */
export default defineConfig({
  testDir: "./tests-e2e",
  timeout: 60_000,
  retries: 0,
  use: {
    baseURL: "http://localhost:5173",
    viewport: { width: 1440, height: 900 },
    launchOptions: {
      executablePath: "D:\\Tools\\Chrome-bin\\chrome.exe",
      headless: true,
    },
  },
  webServer: {
    command: "npx vite --mode dev",
    port: 5173,
    reuseExistingServer: true,
    env: { VITE_MOCK: "1" },
    timeout: 60_000,
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 900 } } },
    { name: "tablet", use: { viewport: { width: 768, height: 1024 } } },
    { name: "mobile", use: { viewport: { width: 375, height: 780 } } },
  ],
});