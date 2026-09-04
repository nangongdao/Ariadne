/**
 * ECharts 按需引入 —— 唯一的 echarts 实例来源。
 *
 * 全量 import "echarts"（或 import "echarts-for-react" 默认入口）会打进 1.1MB，
 * 而全站只用到折线/柱状/饼三种图。图表组件一律用 `echarts-for-react/lib/core`
 * 并注入这里导出的实例，绝不要直接 import "echarts-for-react"。
 * 新增图表类型时在这里补对应的 use() 注册。
 */

import { BarChart, LineChart, PieChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TitleComponent,
  TooltipComponent,
} from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";

import { readTokens } from "@/lib/design-tokens";

echarts.use([
  PieChart,
  LineChart,
  BarChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  MarkLineComponent,
  // Canvas 而非 SVG：数据点多时 Canvas 渲染性能更稳
  CanvasRenderer,
]);

/**
 * 图表主题 —— 从 CSS 令牌派生，随日/夜主题切换。
 *
 * 不再手写色值：色板、坐标轴、提示框全部读 `--kind-*` / `--text` 等令牌
 * （见 design-tokens.ts 为何要用探针解析）。这样"图表与徽标同色即同义"
 * 这条规则由**同一份令牌**保证，而不是靠两处各写一遍再祈祷不漂移。
 *
 * Newsprint 的取舍：柱子不再带圆角（barBorderRadius 归零），折线不平滑
 * —— 这个风格里没有圆角也没有柔化曲线。
 */

const CHART_TOKENS = {
  text: "--text",
  textDim: "--text-dim",
  axis: "--border-strong",
  split: "--border",
  surface: "--bg-elev",
  bg: "--bg",
  k1: "--kind-llm",
  k2: "--kind-loop",
  k3: "--kind-tool",
  k4: "--kind-rag",
  k5: "--kind-code",
  k6: "--kind-eval",
  k7: "--kind-harness",
  k8: "--kind-internal",
} as const;

/**
 * 按当前主题重建并注册图表主题，返回主题名。
 *
 * 名字里带 mode，切换时 ReactEChartsCore 会因 theme prop 变化而重建实例
 * —— ECharts 的主题是初始化时固化的，改 option 不会让旧主题的坐标轴变色。
 */
export function syncChartTheme(mode: "light" | "dark"): string {
  const t = readTokens(CHART_TOKENS);
  const axisDefaults = {
    axisLine: { lineStyle: { color: t.axis } },
    axisTick: { lineStyle: { color: t.axis } },
    axisLabel: {
      color: t.textDim,
      fontSize: 11,
      fontFamily: "JetBrains Mono Variable, monospace",
    },
    splitLine: { lineStyle: { color: t.split } },
    splitArea: { show: false },
  };

  const name = `ariadne-${mode}`;
  echarts.registerTheme(name, {
    // 数据色板与 CSS 的 --kind-* 对齐，图表与徽标同色即同义
    color: [t.k1, t.k2, t.k3, t.k4, t.k5, t.k6, t.k7, t.k8],
    backgroundColor: "transparent",
    textStyle: { fontFamily: "Noto Sans SC Variable, Inter Variable, system-ui, sans-serif" },
    title: {
      textStyle: {
        color: t.text,
        fontSize: 14,
        fontWeight: 700,
        fontFamily: "Noto Sans SC Variable, Inter Variable, system-ui, sans-serif",
      },
    },
    legend: { textStyle: { color: t.textDim }, inactiveColor: t.split },
    tooltip: {
      backgroundColor: t.surface,
      borderColor: t.axis,
      borderWidth: 1,
      // 提示框圆角与卡片几何一致
      borderRadius: 8,
      textStyle: { color: t.text, fontSize: 12 },
      axisPointer: {
        lineStyle: { color: t.axis },
        crossStyle: { color: t.axis },
      },
    },
    categoryAxis: axisDefaults,
    valueAxis: axisDefaults,
    logAxis: axisDefaults,
    timeAxis: axisDefaults,
    line: { symbolSize: 6, smooth: false, lineStyle: { width: 2 } },
    bar: { itemStyle: { barBorderRadius: [4, 4, 0, 0] } },
    pie: { itemStyle: { borderColor: t.bg, borderWidth: 2 } },
  });
  return name;
}

export { echarts };

