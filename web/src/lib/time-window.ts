/**
 * 时间窗选项 —— 成本页与 Span 列表页共用。
 *
 * 必须是同一份：成本页按 30 天统计后下钻到 Span 列表，如果两边的可选值不一致，
 * 落地页的 select 会因为 value 不在 options 里而静默显示成别的档位 ——
 * 于是"实际生效的筛选"和"界面显示的筛选"不是一回事，用户会拿错结论。
 *
 * 上限 24*90 对齐后端 Query(le=24*90)（costs.py get_costs / traces.py list_spans），
 * 越界发出去必然 422。
 */

export interface TimeWindow {
  readonly label: string;
  readonly hours: number;
}

export const TIME_WINDOWS: readonly TimeWindow[] = [
  { label: "1 小时", hours: 1 },
  { label: "24 小时", hours: 24 },
  { label: "7 天", hours: 24 * 7 },
  { label: "30 天", hours: 24 * 30 },
  { label: "90 天", hours: 24 * 90 },
];

export const DEFAULT_WINDOW_HOURS = 24;

/** 后端 le=24*90。手改地址栏能越界，进请求前必须挡住 */
export const MAX_WINDOW_HOURS = 24 * 90;

/**
 * 把小时数说成人话。
 *
 * 下钻带过来的值不一定落在 TIME_WINDOWS 上（用户可以手改地址栏），
 * 所以标签是算出来的而非查表 —— 查不到就显示原始数字比显示"—"有用。
 */
export function formatWindowLabel(hours: number): string {
  const preset = TIME_WINDOWS.find((w) => w.hours === hours);
  if (preset) return preset.label;
  if (hours % 24 === 0) return `${hours / 24} 天`;
  return `${hours} 小时`;
}
