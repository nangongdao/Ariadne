/**
 * 截图脚本 —— README 配图与设计评审用。
 *
 * 连 VITE_MOCK=1 的 dev server（确定性数据），因此产出可复现：
 * 同一次改动前后截图可以逐像素对比。
 *
 * 用法：node scripts/shoot.mjs [--port 5178] [--theme dark|light|both]
 */

import { mkdir } from "node:fs/promises";
import { chromium } from "playwright";

const args = process.argv.slice(2);
const argOf = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};

const PORT = argOf("port", "5178");
const THEME = argOf("theme", "both");
const BASE = `http://localhost:${PORT}`;
const OUT = ".shots";

/** 每张图：路由 + 文件名 + 进入后要等的选择器 + 可选的准备动作 */
const SHOTS = [
  { name: "traces", path: "/traces", wait: "table tbody tr" },
  { name: "loops", path: "/loops", wait: "table tbody tr" },
  { name: "loop-detail", path: "/loops", wait: "table tbody tr", act: "openFirstLoop" },
  { name: "graph-editor", path: "/graphs", wait: "table tbody tr", act: "openFirstGraph" },
  { name: "experiments", path: "/experiments", wait: "table tbody tr" },
  { name: "playground", path: "/playground", wait: "textarea" },
];

const ACTIONS = {
  async openFirstLoop(page) {
    await page.locator("table tbody tr a").first().click();
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(1200); // ECharts 首帧
  },
  async openFirstGraph(page) {
    await page.locator("table tbody tr a").first().click();
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(1200); // React Flow 布局
  },
};

const themes = THEME === "both" ? ["light", "dark"] : [THEME];

const browser = await chromium.launch({
  executablePath: "D:\\Tools\\Chrome-bin\\chrome.exe",
  headless: true,
});

await mkdir(OUT, { recursive: true });

for (const theme of themes) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2, // README 配图在高 DPI 屏上不糊
    colorScheme: theme,
  });
  // 主题偏好在首帧前写入，避免截到"先亮后暗"的闪烁
  await context.addInitScript((t) => {
    localStorage.setItem("ariadne.theme", t);
  }, theme);

  const page = await context.newPage();

  for (const shot of SHOTS) {
    await page.goto(`${BASE}${shot.path}`, { waitUntil: "networkidle" });
    if (shot.wait) {
      await page.waitForSelector(shot.wait, { timeout: 15_000 }).catch(() => {});
    }
    if (shot.act) await ACTIONS[shot.act](page);
    await page.waitForTimeout(500);

    const suffix = themes.length > 1 ? `-${theme}` : "";
    const file = `${OUT}/${shot.name}${suffix}.png`;
    await page.screenshot({ path: file });
    console.log(`✓ ${file}`);
  }

  await context.close();
}

await browser.close();
