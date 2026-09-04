/**
 * 锁定样本变化的归类规则。
 *
 * 这些不变量是从后端 stats.py 的定义推出来的，不是审美选择：
 * flipped_to_pass 要求 delta>0，因此它是 improved 的真子集；
 * total_changed 是 improved+regressed 的派生值。两者若被当成独立类别展示，
 * 同一批样本会被数两遍甚至三遍。规则只在这个文件里，测试也放在这里。
 */

import { describe, expect, it } from "vitest";

import {
  churnEntries,
  churnTotal,
  hasTwoWayChurn,
  lowerIsBetter,
  polarityLabel,
} from "./compare-format";

/** 后端 churn_summary 的真实形状（stats.py:236-242） */
const FULL_CHURN = {
  improved: 10,
  regressed: 4,
  flipped_to_pass: 3,
  flipped_to_fail: 1,
  total_changed: 14,
};

describe("churnEntries", () => {
  it("丢掉 total_changed —— 它等于 improved+regressed，展示就是重复计数", () => {
    const keys = churnEntries(FULL_CHURN).map((e) => e.key);
    expect(keys).not.toContain("total_changed");
  });

  it("按大类紧跟自身子集的顺序排列，不受后端键序影响", () => {
    // 故意打乱输入键序：JSON 对象键序不可依赖
    const scrambled = {
      flipped_to_fail: 1,
      improved: 10,
      total_changed: 14,
      regressed: 4,
      flipped_to_pass: 3,
    };
    expect(churnEntries(scrambled).map((e) => e.key)).toEqual([
      "improved",
      "flipped_to_pass",
      "regressed",
      "flipped_to_fail",
    ]);
  });

  it("只把 flipped_* 标成子集，大类不标", () => {
    const subsetByKey = Object.fromEntries(
      churnEntries(FULL_CHURN).map((e) => [e.key, e.subset]),
    );
    expect(subsetByKey).toEqual({
      improved: false,
      flipped_to_pass: true,
      regressed: false,
      flipped_to_fail: true,
    });
  });

  it("子集继承大类的语义色，不是中性", () => {
    const toneByKey = Object.fromEntries(
      churnEntries(FULL_CHURN).map((e) => [e.key, e.tone]),
    );
    expect(toneByKey).toEqual({
      improved: "good",
      flipped_to_pass: "good",
      regressed: "bad",
      flipped_to_fail: "bad",
    });
  });

  it("后端新增的未知键仍然显示，排在已知键之后并按字母序", () => {
    const withNew = { ...FULL_CHURN, zeroed_out: 2, degraded_severely: 5 };
    const keys = churnEntries(withNew).map((e) => e.key);
    expect(keys).toEqual([
      "improved",
      "flipped_to_pass",
      "regressed",
      "flipped_to_fail",
      "degraded_severely",
      "zeroed_out",
    ]);
  });

  it("未知键回落到原始键名与中性色，不假装认识它", () => {
    const entries = churnEntries({ improved: 1, mystery_bucket: 7 });
    expect(entries).toContainEqual({
      key: "mystery_bucket",
      label: "mystery_bucket",
      value: 7,
      tone: "neutral",
      subset: false,
    });
  });

  it("后端没给的键不补零 —— 凭空多出的 0 会被当成真实结论", () => {
    const keys = churnEntries({ improved: 3 }).map((e) => e.key);
    expect(keys).toEqual(["improved"]);
  });

  it("空 churn 返回空数组，交给调用方渲染缺数据文案", () => {
    expect(churnEntries({})).toEqual([]);
  });
});

describe("churnTotal", () => {
  it("等于 improved+regressed，不叠加子集也不叠加派生值", () => {
    // 10+4=14；若误加 flipped_*(4) 会得 18，若再加 total_changed 会得 32
    expect(churnTotal(FULL_CHURN)).toBe(14);
  });

  it("忽略 total_changed 本身，不依赖后端是否回传它", () => {
    const { total_changed: _omitted, ...withoutDerived } = FULL_CHURN;
    expect(churnTotal(withoutDerived)).toBe(churnTotal(FULL_CHURN));
  });

  it("缺字段按 0 计，不产生 NaN", () => {
    expect(churnTotal({})).toBe(0);
    expect(churnTotal({ improved: 5 })).toBe(5);
  });
});

describe("hasTwoWayChurn", () => {
  it("两个方向都有变化时为真 —— 均值可能持平但引入了新失败模式", () => {
    expect(hasTwoWayChurn({ improved: 8, regressed: 6 })).toBe(true);
  });

  it("单向变化为假", () => {
    expect(hasTwoWayChurn({ improved: 8, regressed: 0 })).toBe(false);
    expect(hasTwoWayChurn({ regressed: 8 })).toBe(false);
  });

  it("只有跨及格线的样本时也算双向", () => {
    expect(hasTwoWayChurn({ flipped_to_pass: 1, flipped_to_fail: 1 })).toBe(true);
  });

  it("无变化为假", () => {
    expect(hasTwoWayChurn({})).toBe(false);
  });
});

describe("指标极性", () => {
  it("成本越低越好，否则降本会被显示成退化", () => {
    expect(lowerIsBetter("cost_per_item")).toBe(true);
    expect(polarityLabel("cost_per_item")).toBe("越低越好");
  });

  it("质量与通过率越高越好", () => {
    expect(lowerIsBetter("composite_quality")).toBe(false);
    expect(polarityLabel("assertion_pass_rate")).toBe("越高越好");
  });

  it("未登记的指标默认越高越好", () => {
    expect(polarityLabel("some_new_metric")).toBe("越高越好");
  });
});
