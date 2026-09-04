import { expect, test } from "@playwright/test";

/**
 * Phase 4 浏览器实测（VITE_MOCK 数据）：核心页面渲染 + 交互守卫 + 响应式。
 * 所有用例在 desktop/tablet/mobile 三档视口下跑（见 playwright.config projects）。
 */

const TOP_LEVEL_ROUTES = [
  "/traces",
  "/spans",
  "/loops",
  "/experiments",
  "/datasets",
  "/graphs",
  "/playground",
  "/models",
  "/costs",
  "/terminal",
  "/settings",
] as const;

test("顶级页面无运行时异常", async ({ page }) => {
  const runtimeErrors: string[] = [];
  page.on("pageerror", (error) => runtimeErrors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") runtimeErrors.push(`console: ${message.text()}`);
  });

  for (const route of TOP_LEVEL_ROUTES) {
    await page.goto(route);
    await page.waitForLoadState("networkidle");
    await expect(page.locator("main")).toBeVisible();
  }

  expect(runtimeErrors).toEqual([]);
});

test("Trace 列表页正常渲染并跳转详情", async ({ page }) => {
  await page.goto("/traces");
  await expect(page.locator("h1")).toContainText("Trace");
  // mock 有 50 条，首屏表格有行
  await expect(page.locator("tbody tr").first()).toBeVisible();
});

test("Trace 详情：树⇄时间轴切换保持折叠与筛选", async ({ page }) => {
  await page.goto("/traces");
  await page.locator("tbody tr").first().click();
  // 等详情页出现「调用树」与「时间轴」切换
  await page.getByRole("tab", { name: "时间轴" }).waitFor();
  // 勾选「只看失败」
  await page.getByText("只看失败").first().click();
  await expect(page.getByText(/失败 span/)).toBeVisible();
  // 切到时间轴再切回
  await page.getByRole("tab", { name: "时间轴" }).click();
  await page.getByRole("tab", { name: "调用树" }).click();
  // 折叠/筛选状态应保持
  await expect(page.getByText(/失败 span/)).toBeVisible();
});

test("图编辑器：侧栏导航丢未保存修改会被拦下", async ({ page }) => {
  await page.goto("/graphs");
  // 列表行本身不导航，点名称链接进编辑器
  await page.getByRole("link", { name: "rag-doc-quality" }).click();
  const canvas = page.locator(".react-flow");
  await canvas.waitFor({ timeout: 15_000 });
  // llm 节点带可编辑参数（model/temperature，textarea），点它弹出配置面板
  const draftNode = page.locator(".react-flow__node", { hasText: "draft" }).first();
  await draftNode.click();
  const paramField = page.locator(".config-panel").filter({ hasText: "参数" }).first();
  await expect(paramField).toBeVisible({ timeout: 15_000 });
  const modelBox = paramField.locator("textarea").first();
  await modelBox.fill("gpt-4.1-turbo");
  // 参数在 blur 时提交（onBlur={apply}）。toHaveValue 先确认 React 受控
  // state 已回显（fill 后立即 blur 会读到旧 draft，参数不变也就不会置脏）。
  await expect(modelBox).toHaveValue("gpt-4.1-turbo");
  await modelBox.press("Tab");
  // 脏后点侧栏「Loop」（精确 href，避免命中 Ctrl+K 面板的「Loop」项；
  // 小屏侧栏是抽屉，先展开）
  const loopLink = page.locator('a[href="/loops"]').first();
  const toggle = page.getByRole("button", { name: "打开导航" });
  if (await toggle.count()) {
    await toggle.first().click();
  }
  await loopLink.click();
  await expect(page.getByText("还有未保存的修改")).toBeVisible();
  await expect(page.getByText("留在本页")).toBeVisible();
});

test("模型配置：填草稿后跳转会提示", async ({ page }) => {
  // 守卫用 window.confirm（原生对话框），Playwright 自动 dismiss 即视为「取消离开」。
  // 断言就在于 confirm 被调用过、且页面停留在模型配置页。
  // 注意：tablet/mobile 下侧栏是抽屉，点导航前要先展开。
  await page.goto("/models");
  await page.getByText("新建配置").first().click();
  const nameInput = page.getByLabel("显示名称");
  await nameInput.fill("测试模型");
  let confirmed = 0;
  page.on("dialog", (dialog) => {
    if (dialog.type() === "confirm") confirmed += 1;
    void dialog.dismiss();
  });
  // 未保存内容时点侧栏「Trace」（小屏下侧栏是抽屉，先展开）
  const traceLink = page.locator("a", { hasText: "Trace" }).first();
  const toggle = page.getByRole("button", { name: "打开导航" });
  if (await toggle.count()) {
    await toggle.first().click(); // 桌面按钮不存在（count=0），小屏才需要点
  }
  await traceLink.click();
  await expect
    .poll(() => confirmed, { timeout: 5000 })
    .toBeGreaterThanOrEqual(1);
  // 取消后仍留在模型配置页（h2「新建模型配置」视作证明表单还开着）
  await expect(page.getByRole("heading", { name: "新建模型配置" })).toBeVisible();
  await expect(page.getByLabel("显示名称")).toHaveValue("测试模型");
});

test("数据集：版本行可展开预览样本", async ({ page }) => {
  await page.goto("/datasets");
  // 列表面没有详情页，点数据集名展开版本列表
  await page.getByRole("button", { name: "qa-support" }).click();
  // 第一个版本的「预览样本」出现后才点，避免点到后加载的第二行的
  const preview = page.getByRole("button", { name: "预览样本" }).first();
  await expect(preview).toBeVisible();
  await preview.click();
  await expect(page.getByText(/条 · 哈希/)).toBeVisible();
});

test("Loop 详情：失败断言展示证据而非裸 id", async ({ page }) => {
  await page.goto("/loops");
  // mock 第一个 loop 是「已达标」（无失败断言），选「原地打转」的那行进详情。
  // 状态标签的 span 在该行第一个 td 的 Link 之后，用 closest('tr') 取整行。
  await page.locator(".status-tag-stalled").first().click();
  await page.locator(".status-tag-stalled").first().locator("xpath=ancestor::tr").getByRole("link").first().click();
  // 轮次表有失败断言，含 hint 或证据
  await expect(page.locator(".failed-assertions").first()).toBeVisible({ timeout: 15_000 });
  await expect(page.locator(".failed-evidence").first()).toBeVisible();
});

test("主题切换：夜间真的改变渲染色并跨刷新保持", async ({ page }, testInfo) => {
  await page.goto("/traces");

  // 基线：跟随系统时 Playwright 默认 light，页面应是浅底
  const bodyBg = () =>
    page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  const lightBg = await bodyBg();

  // 断言渲染结果而不只断言 data-theme：只查属性的话，
  // 即使没有任何 CSS 接到这个属性上，用例照样会绿。
  // 窄屏（tablet/mobile）是单键循环，宽屏（desktop）是三态分段控件。
  const isNarrow = testInfo.project.name !== "desktop";
  if (isNarrow) {
    // 循环按钮：当前「跟随系统」，点一下→日间，再点→夜间
    const cycle = page.getByRole("button", { name: /配色主题/ });
    await expect(cycle).toBeVisible();
    await cycle.click(); // system → light
    await cycle.click(); // light → dark
  } else {
    await page.getByRole("button", { name: "夜间" }).click();
  }
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  const darkBg = await bodyBg();
  expect(darkBg).not.toBe(lightBg);

  // 夜间底色应当真的更暗（比较相对亮度之和，避免写死具体色值）
  const sum = (rgb: string) =>
    (rgb.match(/\d+/g) ?? []).slice(0, 3).reduce((a, v) => a + Number(v), 0);
  expect(sum(darkBg)).toBeLessThan(sum(lightBg));

  // 选择要落盘并在刷新后仍生效
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  expect(await bodyBg()).toBe(darkBg);

  // 切回「跟随系统」应摘掉 data-theme，交回 prefers-color-scheme
  if (isNarrow) {
    const cycle = page.getByRole("button", { name: /配色主题/ });
    await cycle.click(); // dark → system
  } else {
    await expect(page.getByRole("button", { name: "夜间" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await page.getByRole("button", { name: "跟随系统" }).click();
  }
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", /.*/);
  expect(await bodyBg()).toBe(lightBg);
});

test("移动端：侧栏展开为抽屉且可关闭", async ({ page }, testInfo) => {
  // 仅 mobile 档跑
  test.skip(testInfo.project.name !== "mobile", "只测移动端抽屉");
  await page.goto("/traces");
  const toggle = page.getByRole("button", { name: "打开导航" });
  await expect(toggle).toBeVisible();
  await toggle.click();
  await expect(page.locator(".sidebar")).toHaveClass(/open/);
  // Escape 可关
  await page.keyboard.press("Escape");
  await expect(page.locator(".sidebar")).not.toHaveClass(/open/);
});
