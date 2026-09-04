/**
 * 图表封装 —— 全站唯一的 ECharts 挂载点。
 *
 * 用 esm/core 而非默认入口：默认入口自带全量 echarts（1.1MB），
 * lib/ 是 CJS 不利于 tree-shaking。图表实例与主题都从 @/lib/echarts 注入。
 *
 * 主题随日/夜切换重建：ECharts 的主题在初始化时固化，只改 option 不会让
 * 已渲染的坐标轴变色，所以 theme 名字里带 mode，切换时触发实例重建。
 */

import ReactEChartsCore from "echarts-for-react/esm/core";
import { useMemo } from "react";

import { echarts, syncChartTheme } from "@/lib/echarts";
import { useTheme } from "@/lib/theme";

interface ChartProps {
  /** 必须是 memo 过的稳定引用，否则每次父组件渲染都触发一次全量 setOption */
  option: Record<string, unknown>;
  height?: number;
  /** 图表标题，渲染成 figcaption 而非画布内文字，可被屏幕阅读器读到 */
  title?: string;
  /** 数据形态变化（如切换分组）时传 true，避免残留上一份 series */
  notMerge?: boolean;
  /** 无障碍：图表的文字描述，供屏幕阅读器替代视觉信息 */
  ariaLabel?: string;
}

export function Chart({
  option,
  height = 240,
  title,
  notMerge = false,
  ariaLabel,
}: ChartProps) {
  const { resolved } = useTheme();
  // 在渲染期同步注册：此时 <html data-theme> 已是目标主题，
  // 读令牌拿到的就是该主题下的解析值
  const theme = useMemo(() => syncChartTheme(resolved), [resolved]);

  return (
    <figure className="chart-card">
      {title && <figcaption className="chart-card-title">{title}</figcaption>}
      <div
        className="chart-canvas"
        role="img"
        aria-label={ariaLabel ?? title ?? "数据图表"}
      >
        <ReactEChartsCore
          echarts={echarts}
          theme={theme}
          option={option}
          style={{ height }}
          notMerge={notMerge}
          lazyUpdate
        />
      </div>
    </figure>
  );
}
