/**
 * 生成 README 截图
 * 使用 Playwright 访问本地开发服务器并截图
 */

import { chromium } from 'playwright';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const screenshots = [
  { url: 'http://127.0.0.1:5173/traces', path: '.shots/traces-light.png', theme: 'light', width: 1920, height: 1080 },
  { url: 'http://127.0.0.1:5173/graphs', path: '.shots/graph-editor-dark.png', theme: 'dark', width: 1920, height: 1080 },
  { url: 'http://127.0.0.1:5173/loops', path: '.shots/loop-detail-light.png', theme: 'light', width: 1920, height: 1080 },
  { url: 'http://127.0.0.1:5173/experiments', path: '.shots/experiments-dark.png', theme: 'dark', width: 1920, height: 1080 }
];

(async () => {
  const browser = await chromium.launch({ headless: true });

  for (const shot of screenshots) {
    const context = await browser.newContext({
      viewport: { width: shot.width, height: shot.height },
      deviceScaleFactor: 1
    });

    const page = await context.newPage();

    // 设置主题
    await page.addInitScript((theme) => {
      localStorage.setItem('ariadne-theme', theme);
    }, shot.theme);

    console.log(`访问 ${shot.url}，主题：${shot.theme}`);
    await page.goto(shot.url, { waitUntil: 'networkidle', timeout: 30000 });

    // 等待内容渲染
    await page.waitForTimeout(2000);

    // 截图
    const outputPath = path.join(__dirname, '..', shot.path);
    await page.screenshot({ path: outputPath, fullPage: false });
    console.log(`✓ 已生成：${shot.path}`);

    await context.close();
  }

  await browser.close();
  console.log('\n所有截图已生成完毕');
})();
