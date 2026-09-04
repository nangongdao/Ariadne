/**
 * Chart 的懒加载包装 —— 按路由分割 echarts chunk（599KB → 首屏不加载）。
 *
 * 图表库只在 2 个页面使用（CostPage / LoopDetailPage），不该在首屏就加载。
 * 这个组件用 React.lazy 动态导入真正的 Chart，webpack/vite 会把 echarts
 * 单独打成 async chunk，访问含图表的页面时才下载。
 *
 * 用法：把 `import { Chart }` 改成 `import { Chart } from "@/components/ChartLazy"`，
 * 其余代码不变（props 接口完全一致）。
 */

import { type ComponentProps, Suspense, lazy } from "react";

const ChartImpl = lazy(() =>
  import("@/components/Chart").then((m) => ({ default: m.Chart })),
);

type ChartProps = ComponentProps<typeof ChartImpl>;

export function Chart(props: ChartProps) {
  return (
    <Suspense
      fallback={
        <div
          className="skeleton"
          style={{ height: props.height ?? 240, borderRadius: 8 }}
          aria-hidden
          role="presentation"
        />
      }
    >
      <ChartImpl {...props} />
    </Suspense>
  );
}
