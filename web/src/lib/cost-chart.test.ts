import { describe, expect, it } from "vitest";

import type { CostBucket } from "@/api/types";
import { buildCostChartOption, isTimeDimension } from "@/lib/cost-chart";

function bucket(key: Record<string, string>, cost: string): CostBucket {
  return {
    key,
    span_count: 1,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    reasoning_tokens: 0,
    cost_usd: cost,
  };
}

/** ECharts option 是无类型的字面量，测试里按需窄化 */
function series(option: Record<string, unknown>): Array<Record<string, unknown>> {
  return option["series"] as Array<Record<string, unknown>>;
}

function axisData(option: Record<string, unknown>): string[] {
  return (option["xAxis"] as Record<string, unknown>)["data"] as string[];
}

describe("isTimeDimension", () => {
  it("认得后端的三个时间维度", () => {
    expect(isTimeDimension("minute")).toBe(true);
    expect(isTimeDimension("hour")).toBe(true);
    expect(isTimeDimension("day")).toBe(true);
  });

  it("归因维度不是时间维度", () => {
    expect(isTimeDimension("model_request")).toBe(false);
    expect(isTimeDimension("provider")).toBe(false);
  });
});

describe("buildCostChartOption 占比环", () => {
  it("不展开且主维度非时间时给出 pie", () => {
    const option = buildCostChartOption({
      buckets: [
        bucket({ model_request: "gpt-4o" }, "1.5"),
        bucket({ model_request: "claude" }, "0.5"),
      ],
      groupBy: "model_request",
      expansion: "",
    });
    const first = series(option)[0];
    expect(first?.["type"]).toBe("pie");
    expect(first?.["data"]).toEqual([
      { name: "gpt-4o", value: 1.5 },
      { name: "claude", value: 0.5 },
    ]);
  });

  it("维度值缺失时落到占位符而不是 undefined", () => {
    const option = buildCostChartOption({
      buckets: [bucket({}, "1")],
      groupBy: "model_request",
      expansion: "",
    });
    const data = series(option)[0]?.["data"] as Array<{ name: string }>;
    expect(data[0]?.name).toBe("—");
  });
});

describe("buildCostChartOption 单条时序", () => {
  it("按时间正序重排 —— 后端是按金额倒序返回的", () => {
    const option = buildCostChartOption({
      buckets: [
        bucket({ hour: "2026-08-29 12:00:00" }, "9"),
        bucket({ hour: "2026-08-29 09:00:00" }, "1"),
        bucket({ hour: "2026-08-29 10:00:00" }, "5"),
      ],
      groupBy: "hour",
      expansion: "",
    });
    expect(axisData(option)).toEqual([
      "2026-08-29 09:00",
      "2026-08-29 10:00",
      "2026-08-29 12:00",
    ]);
    expect(series(option)[0]?.["data"]).toEqual([1, 5, 9]);
  });
});

describe("buildCostChartOption 时间展开", () => {
  const buckets = [
    bucket({ model_request: "gpt-4o", hour: "2026-08-29 10:00:00" }, "2"),
    bucket({ model_request: "gpt-4o", hour: "2026-08-29 11:00:00" }, "3"),
    bucket({ model_request: "claude", hour: "2026-08-29 11:00:00" }, "7"),
  ];

  it("每个维度值一条线，横轴是所有时间点的并集", () => {
    const option = buildCostChartOption({
      buckets,
      groupBy: "model_request",
      expansion: "hour",
    });
    expect(axisData(option)).toEqual(["2026-08-29 10:00", "2026-08-29 11:00"]);
    expect(series(option).map((s) => s["name"])).toEqual(["gpt-4o", "claude"]);
  });

  it("缺失的时间点补 0 —— 没有 rollup 行就是那个时段真的没花钱", () => {
    const option = buildCostChartOption({
      buckets,
      groupBy: "model_request",
      expansion: "hour",
    });
    const claude = series(option).find((s) => s["name"] === "claude");
    // claude 在 10:00 没有桶，必须是 0 而不是 null/undefined，否则折线会断
    expect(claude?.["data"]).toEqual([0, 7]);
  });

  it("同名维度值的多行合并而不是互相顶掉", () => {
    const option = buildCostChartOption({
      buckets: [
        bucket({ hour: "2026-08-29 10:00:00" }, "2"),
        bucket({ hour: "2026-08-29 10:00:00" }, "3"),
      ],
      groupBy: "model_request",
      expansion: "hour",
    });
    expect(series(option)).toHaveLength(1);
    expect(series(option)[0]?.["data"]).toEqual([5]);
  });

  it("展开优先于主维度形态：主维度是时间也不会退回单线", () => {
    const option = buildCostChartOption({
      buckets: [bucket({ day: "2026-08-29", hour: "2026-08-29 10:00:00" }, "1")],
      groupBy: "day",
      expansion: "hour",
    });
    expect(series(option)[0]?.["type"]).toBe("line");
    expect(series(option)[0]).toHaveProperty("name");
  });
});

describe("buildCostChartOption 坏数据", () => {
  it("cost_usd 解析不出数字时记 0，不把 NaN 传进图表", () => {
    const option = buildCostChartOption({
      buckets: [bucket({ model_request: "x" }, "not-a-number")],
      groupBy: "model_request",
      expansion: "",
    });
    const data = series(option)[0]?.["data"] as Array<{ value: number }>;
    expect(data[0]?.value).toBe(0);
  });

  it("空 buckets 不抛异常", () => {
    for (const expansion of ["", "hour"]) {
      expect(() =>
        buildCostChartOption({ buckets: [], groupBy: "model_request", expansion }),
      ).not.toThrow();
    }
  });
});
