/**
 * 成本页的 ECharts option 构造。
 *
 * 从页面里拆出来的原因不是行数，是这里全是"哪个数进哪根轴"的判断 ——
 * 放在组件里只能靠肉眼看图验证，拆出来才能拿 buckets 直接断言。
 */

import type { CostBucket } from "@/api/types";

/** 后端返回的 cost_usd 是字符串（Decimal 序列化），坏值按 0 记而不是 NaN 传进图表 */
function toAmount(raw: string): number {
  const value = Number.parseFloat(raw);
  return Number.isFinite(value) ? value : 0;
}

/** 时间桶的 key 形如 "2026-08-29 14:00:00"，图上只需到分钟 */
function shortTime(value: string): string {
  return value.slice(0, 16);
}

export interface CostChartInput {
  buckets: CostBucket[];
  /** 主分组维度的 key 名（model_request / provider / hour / day） */
  groupBy: string;
  /** 时间展开维度；空串表示不展开 */
  expansion: string;
}

/**
 * 每个维度值一条时间序列。
 *
 * 缺失的 (维度, 时间) 补 0 而非 null：cost_rollup 只在有调用时写行，
 * 没有行就是那个时段真的没花钱。断线会被读成"数据丢了"。
 */
function buildExpandedSeries({ buckets, groupBy, expansion }: CostChartInput) {
  const times = [...new Set(buckets.map((b) => b.key[expansion] ?? ""))].sort();
  const byKey = new Map<string, Map<string, number>>();

  for (const bucket of buckets) {
    const name = bucket.key[groupBy] ?? "—";
    const slot = byKey.get(name) ?? new Map<string, number>();
    const time = bucket.key[expansion] ?? "";
    // 累加而非覆盖：维度值缺失时都会落到 "—"，这些行必须合并而不是互相顶掉
    slot.set(time, (slot.get(time) ?? 0) + toAmount(bucket.cost_usd));
    byKey.set(name, slot);
  }

  return {
    tooltip: { trigger: "axis" },
    legend: { bottom: 0, type: "scroll" },
    grid: { left: 60, right: 24, top: 24, bottom: 64 },
    xAxis: {
      type: "category",
      data: times.map(shortTime),
      axisLabel: { rotate: 45, fontSize: 10 },
    },
    yAxis: { type: "value", name: "USD" },
    series: [...byKey.entries()].map(([name, slot]) => ({
      name,
      type: "line",
      data: times.map((t) => slot.get(t) ?? 0),
      smooth: true,
      showSymbol: false,
    })),
  };
}

/** 单条时间序列：主维度本身就是时间，无需再分组 */
function buildTimeSeries(buckets: CostBucket[], groupBy: string) {
  // 后端按金额倒序返回，时序图必须自己按时间正序排，否则折线来回折
  const paired = buckets
    .map((bucket) => ({
      label: bucket.key[groupBy] ?? "—",
      cost: toAmount(bucket.cost_usd),
    }))
    .sort((a, b) => a.label.localeCompare(b.label));

  return {
    tooltip: { trigger: "axis" },
    grid: { left: 60, right: 24, top: 24, bottom: 48 },
    xAxis: {
      type: "category",
      data: paired.map((item) => shortTime(item.label)),
      axisLabel: { rotate: 45, fontSize: 10 },
    },
    yAxis: { type: "value", name: "USD" },
    series: [
      {
        type: "line",
        data: paired.map((item) => item.cost),
        smooth: true,
        areaStyle: { opacity: 0.15 },
        itemStyle: { color: "#4c8dff" },
      },
    ],
  };
}

/** 占比环图：维度值之间比大小，不涉及时间 */
function buildShareSeries(buckets: CostBucket[], groupBy: string) {
  return {
    tooltip: { trigger: "item", formatter: "{b}: ${c} ({d}%)" },
    legend: { bottom: 0, type: "scroll" },
    series: [
      {
        type: "pie",
        radius: ["45%", "70%"],
        data: buckets.map((bucket) => ({
          name: bucket.key[groupBy] ?? "—",
          value: toAmount(bucket.cost_usd),
        })),
        label: { formatter: "{b}\n{d}%" },
      },
    ],
  };
}

export const TIME_DIMENSIONS: ReadonlySet<string> = new Set(["minute", "hour", "day"]);

export function isTimeDimension(dimension: string): boolean {
  return TIME_DIMENSIONS.has(dimension);
}

/** 三种形态择一：展开多线 → 单线时序 → 占比环 */
export function buildCostChartOption(input: CostChartInput): Record<string, unknown> {
  if (input.expansion) return buildExpandedSeries(input);
  if (isTimeDimension(input.groupBy)) {
    return buildTimeSeries(input.buckets, input.groupBy);
  }
  return buildShareSeries(input.buckets, input.groupBy);
}
