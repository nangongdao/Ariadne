/**
 * 指标对比表。
 *
 * 每行都要能独立读懂"哪边算好"：指标名后跟极性标签，
 * 判定列用图标 + 文字双通道，不靠颜色单独表意。
 */

import {
  CircleQuestionMark,
  Minus,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { memo } from "react";

import type { CompareDirection, CompareStat } from "@/api/types";
import {
  ciCrossesZero,
  DIRECTION_LABELS,
  DIRECTION_TONES,
  lowerIsBetter,
  metricLabel,
  polarityLabel,
  signed,
  signedPercent,
  verdictText,
} from "@/lib/compare-format";

const DIRECTION_ICONS: Record<CompareDirection, LucideIcon> = {
  improved: TrendingUp,
  regressed: TrendingDown,
  unchanged: Minus,
  inconclusive: CircleQuestionMark,
};

const TONE_CLASS = {
  good: "delta-up",
  bad: "delta-down",
  neutral: "delta-flat",
} as const;

export function CompareStatsTable({ stats }: { stats: CompareStat[] }) {
  if (stats.length === 0) {
    return (
      <div className="callout callout-warn" role="status">
        <strong>没有可对比的指标</strong>
        <p>
          后端未返回任何指标。常见原因：两侧实验的评分指标不同名，或样本全部生成失败。
          请先确认两侧都已跑完并产出了同名指标，再重新对比。
        </p>
      </div>
    );
  }

  return (
    <table className="data-table compare-table">
      <thead>
        <tr>
          <th scope="col">指标</th>
          <th scope="col" className="num">
            baseline
          </th>
          <th scope="col" className="num">
            current
          </th>
          <th scope="col" className="num">
            变化
          </th>
          <th scope="col">95% 置信区间</th>
          <th scope="col" className="num">
            样本
          </th>
          <th scope="col">判定</th>
        </tr>
      </thead>
      <tbody>
        {stats.map((stat) => (
          <StatRow key={stat.metric} stat={stat} />
        ))}
      </tbody>
    </table>
  );
}

const StatRow = memo(function StatRow({ stat }: { stat: CompareStat }) {
  const tone = DIRECTION_TONES[stat.direction];
  const Icon = DIRECTION_ICONS[stat.direction];
  const noSample = stat.sample_count === 0;

  return (
    <tr>
      <td>
        <span className="metric-name">{metricLabel(stat.metric)}</span>
        <span className="metric-polarity">
          {lowerIsBetter(stat.metric) ? "↓" : "↑"} {polarityLabel(stat.metric)}
        </span>
      </td>
      <td className="num">{noSample ? "—" : stat.baseline_mean.toFixed(4)}</td>
      <td className="num">{noSample ? "—" : stat.current_mean.toFixed(4)}</td>
      <td className="num">
        {noSample ? (
          "—"
        ) : (
          <>
            {signed(stat.delta)}
            <span className="hint"> ({signedPercent(stat.delta_pct)})</span>
          </>
        )}
      </td>
      <td>
        {noSample ? (
          <span className="hint">无样本</span>
        ) : (
          <>
            <span className="mono">
              [{signed(stat.ci_low)}, {signed(stat.ci_high)}]
            </span>
            {ciCrossesZero(stat) && (
              <span className="badge badge-warn" title="区间跨 0，方向可能反过来">
                跨 0
              </span>
            )}
          </>
        )}
      </td>
      <td className="num">{stat.sample_count}</td>
      <td>
        <span className={`verdict ${TONE_CLASS[tone]}`} title={verdictText(stat)}>
          <Icon size={13} aria-hidden />
          {DIRECTION_LABELS[stat.direction]}
        </span>
        {!stat.significant && !noSample && (
          <span className="badge badge-warn" title="置信区间跨 0，尚不能断定方向">
            不显著
          </span>
        )}
      </td>
    </tr>
  );
});
