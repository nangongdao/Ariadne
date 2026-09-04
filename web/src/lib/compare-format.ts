/**
 * 实验对比的纯逻辑：中文标签、指标极性、样本变化归类、纯文本导出。
 *
 * 不含 React —— 判定"哪边算好"的规则只此一处，表格、卡片、剪贴板
 * 三种展示共用，避免同一个数字在不同地方显示成相反的结论。
 */

import type { CompareDirection, CompareResponse, CompareStat } from "@/api/types";

/** 语义倾向。neutral 用于"既不好也不坏"，不能并进 bad，否则无变化会被标红。 */
export type CompareTone = "good" | "bad" | "neutral";

const METRIC_LABELS: Record<string, string> = {
  composite_quality: "复合质量分",
  assertion_pass_rate: "断言通过率",
  cost_per_item: "单样本成本",
};

/** 越低越好的指标。新增此类指标必须登记，否则会被当成越高越好而把降本显示为退化。 */
const LOWER_IS_BETTER = new Set(["cost_per_item"]);

export const DIRECTION_LABELS: Record<CompareDirection, string> = {
  improved: "改善",
  regressed: "退化",
  unchanged: "无显著变化",
  inconclusive: "样本不足",
};

export const DIRECTION_TONES: Record<CompareDirection, CompareTone> = {
  improved: "good",
  regressed: "bad",
  unchanged: "neutral",
  inconclusive: "neutral",
};

/** exit_code 语义来自后端契约：0 通过 / 1 有退化 / 2 配置或数据问题 */
const EXIT_CODE_LABELS: Record<number, string> = {
  0: "通过",
  1: "有退化",
  2: "配置或数据问题",
};

/**
 * "其中"前缀不是修辞：flipped_to_pass 要求 delta>0，是 improved 的真子集
 * （stats.py SampleDiff）。四项并列会被读成互斥的四类，于是"变好 10 /
 * 转为通过 3"变成 13 个样本 —— 实际是 10 个里有 3 个跨过了及格线。
 */
const CHURN_LABELS: Record<string, string> = {
  improved: "变好",
  regressed: "变差",
  flipped_to_pass: "其中转为通过",
  flipped_to_fail: "其中转为失败",
};

const CHURN_TONES: Record<string, CompareTone> = {
  improved: "good",
  regressed: "bad",
  flipped_to_pass: "good",
  flipped_to_fail: "bad",
};

/** 展示顺序：每个大类紧跟自己的子集。后端 JSON 键序不稳定，靠它排卡片会乱跳。 */
const CHURN_ORDER = ["improved", "flipped_to_pass", "regressed", "flipped_to_fail"];

/**
 * 后端派生字段，不进卡片：total_changed = improved + regressed
 * （stats.py churn_summary），当卡片显示就是同一批样本数第二次露面。
 */
const CHURN_DERIVED = new Set(["total_changed"]);

/** 子集项，缩进显示以体现包含关系。 */
const CHURN_SUBSET = new Set(["flipped_to_pass", "flipped_to_fail"]);

export interface ChurnEntry {
  key: string;
  label: string;
  value: number;
  tone: CompareTone;
  /** true 表示它被上一个同色大类包含，不能与之相加 */
  subset: boolean;
}

export function metricLabel(metric: string): string {
  return METRIC_LABELS[metric] ?? metric;
}

export function lowerIsBetter(metric: string): boolean {
  return LOWER_IS_BETTER.has(metric);
}

/** 极性提示。差值的正负本身不含褒贬，必须挨着指标名说清哪个方向算好。 */
export function polarityLabel(metric: string): string {
  return lowerIsBetter(metric) ? "越低越好" : "越高越好";
}

export function exitCodeLabel(code: number): string {
  return EXIT_CODE_LABELS[code] ?? "未知";
}

export function signed(value: number, digits = 4): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}`;
}

export function signedPercent(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(1)}%`;
}

/**
 * 判定的完整读法，同时给 aria-label 用。
 * 只有颜色和箭头不够：读屏用户和色盲用户都得从文字里拿到同样的结论。
 */
export function verdictText(stat: CompareStat): string {
  const parts = [DIRECTION_LABELS[stat.direction]];
  if (!stat.significant) parts.push("差异不显著");
  parts.push(polarityLabel(stat.metric));
  return parts.join("，");
}

/**
 * 置信区间是否跨 0。跨 0 意味着方向可能反过来，
 * 后端的 significant 已表达此意，这里只为区间本身补一句可读说明。
 */
export function ciCrossesZero(stat: CompareStat): boolean {
  return stat.ci_low <= 0 && stat.ci_high >= 0;
}

/** 已知键按固定顺序排前，未知键按字母序排后 —— 后端新增字段不能被静默丢掉。 */
export function churnEntries(churn: Record<string, number>): ChurnEntry[] {
  const known = CHURN_ORDER.filter((key) => key in churn);
  const extra = Object.keys(churn)
    .filter((key) => !CHURN_ORDER.includes(key) && !CHURN_DERIVED.has(key))
    .sort();
  return [...known, ...extra].map((key) => ({
    key,
    label: CHURN_LABELS[key] ?? key,
    value: churn[key] ?? 0,
    tone: CHURN_TONES[key] ?? "neutral",
    subset: CHURN_SUBSET.has(key),
  }));
}

/** 双向都有变化：均值可能持平却引入了新失败模式，是最该被看到的信号。 */
export function hasTwoWayChurn(churn: Record<string, number>): boolean {
  const good = (churn.improved ?? 0) + (churn.flipped_to_pass ?? 0);
  const bad = (churn.regressed ?? 0) + (churn.flipped_to_fail ?? 0);
  return good > 0 && bad > 0;
}

/**
 * 发生变化的样本数。只能是 improved + regressed —— 这两项互斥且已覆盖全部变化；
 * 把 flipped_* 或 total_changed 也加进来会让同一个样本数出现两三次。
 */
export function churnTotal(churn: Record<string, number>): number {
  return (churn.improved ?? 0) + (churn.regressed ?? 0);
}

/** 纯文本导出：report_text 是后端给的权威报告，前端补上两侧 judge 与门禁结论。 */
export function buildClipboardText(
  data: CompareResponse,
  baselineId: string,
  currentId: string,
  baselineJudgeModels: string[],
  currentJudgeModels: string[],
): string {
  const lines = [
    `实验对比 ${baselineId} → ${currentId}`,
    `数据集: ${data.dataset_ref}`,
    `门禁: ${data.passed ? "通过" : "未通过"}（exit_code=${data.exit_code} ${exitCodeLabel(
      data.exit_code,
    )}）`,
    `baseline judge: ${baselineJudgeModels.join(", ") || "—"}`,
    `current judge: ${currentJudgeModels.join(", ") || "—"}`,
    "",
  ];

  if (data.stats.length === 0) {
    lines.push("（后端未返回可对比的指标）");
  } else {
    lines.push("指标\t方向\tbaseline\tcurrent\t变化\t变化%\t95%CI\t判定\t显著\t样本");
    for (const s of data.stats) {
      lines.push(
        [
          metricLabel(s.metric),
          polarityLabel(s.metric),
          s.baseline_mean.toFixed(4),
          s.current_mean.toFixed(4),
          signed(s.delta),
          signedPercent(s.delta_pct),
          `[${signed(s.ci_low)}, ${signed(s.ci_high)}]`,
          DIRECTION_LABELS[s.direction],
          s.significant ? "是" : "否",
          String(s.sample_count),
        ].join("\t"),
      );
    }
  }

  lines.push("", `样本变化（共 ${churnTotal(data.churn)} 个样本分数变化）`);
  const entries = churnEntries(data.churn);
  if (entries.length === 0) {
    lines.push("  （后端未返回样本变化数据）");
  } else {
    for (const e of entries) lines.push(`  ${e.label}: ${e.value}`);
  }
  if (hasTwoWayChurn(data.churn)) {
    lines.push("  ⚠ 双向变化：有样本变好也有样本变差，均值可能掩盖新的失败模式");
  }

  if (data.violations.length > 0) {
    lines.push("", "门禁违规");
    for (const v of data.violations) lines.push(`  - ${v}`);
  }
  if (data.warnings.length > 0) {
    lines.push("", "警告");
    for (const w of data.warnings) lines.push(`  - ${w}`);
  }
  if (data.flipped_to_fail.length > 0) {
    lines.push("", `转为失败的样本（${data.flipped_to_fail.length}）`);
    for (const id of data.flipped_to_fail) lines.push(`  ${id}`);
  }
  if (data.report_text.trim() !== "") {
    lines.push("", "---- 后端报告 ----", data.report_text.trimEnd());
  }
  return lines.join("\n");
}
