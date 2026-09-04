/**
 * 样本变化（churn）区块。
 *
 * 均值持平可能掩盖"一半变好一半变差"，那通常意味着引入了新的失败模式，
 * 所以双向变化必须显著提示。只有「转为失败」有明细可下钻，
 * 也只有它做成可点卡片 —— 其余卡片不给 hover 假象。
 */

import { ChevronRight, Copy, Minus, TrendingDown, TrendingUp } from "lucide-react";
import { memo, useCallback, useMemo, useRef, useState } from "react";

import { toast } from "@/components/Toast";
import type { ChurnEntry, CompareTone } from "@/lib/compare-format";
import { churnEntries, churnTotal, hasTwoWayChurn } from "@/lib/compare-format";

/** 明细列表最多渲染这么多条，其余靠「复制全部」拿走 —— 上千条 li 会拖垮滚动 */
const FLIP_LIST_LIMIT = 50;

const TONE_META = {
  good: { cls: "delta-up", Icon: TrendingUp, label: "有利" },
  bad: { cls: "delta-down", Icon: TrendingDown, label: "不利" },
  neutral: { cls: "delta-flat", Icon: Minus, label: "中性" },
} as const satisfies Record<CompareTone, unknown>;

export function CompareChurnBlock({
  churn,
  flippedToFail,
}: {
  churn: Record<string, number>;
  flippedToFail: string[];
}) {
  const [flipOpen, setFlipOpen] = useState(false);
  const flipSummaryRef = useRef<HTMLElement>(null);
  const entries = useMemo(() => churnEntries(churn), [churn]);
  const twoWay = hasTwoWayChurn(churn);
  const total = churnTotal(churn);

  // 点「转为失败」卡片 → 展开明细并把焦点落到 summary 上，
  // 否则键盘用户点完不知道焦点在哪、内容展开在哪
  const openFlipDetails = useCallback(() => {
    setFlipOpen(true);
    window.setTimeout(() => flipSummaryRef.current?.focus(), 0);
  }, []);

  const copyFlipped = useCallback(() => {
    void navigator.clipboard
      .writeText(flippedToFail.join("\n"))
      .then(() => toast.success(`已复制 ${flippedToFail.length} 个样本 ID`))
      .catch(() => toast.error("复制失败，请手动选择文本"));
  }, [flippedToFail]);

  if (entries.length === 0) {
    return (
      <div className="detail-section">
        <h3>样本变化</h3>
        <p className="hint">
          后端未返回样本变化数据。逐样本比对需要两侧都保留了样本级评分，
          若只回传了汇总指标则无法给出这部分。
        </p>
      </div>
    );
  }

  if (total === 0) {
    return (
      <div className="detail-section">
        <h3>样本变化</h3>
        <p className="hint">没有任何样本的分数发生变化，两次运行结果一致。</p>
      </div>
    );
  }

  return (
    <div className="detail-section">
      <h3>
        样本变化
        <span className="churn-total">共 {total} 个样本分数变化</span>
        {twoWay && (
          <span className="badge badge-warn" title="均值可能掩盖了新的失败模式">
            双向变化
          </span>
        )}
      </h3>

      <div className="churn-grid">
        {entries.map((entry) => {
          const drillable = entry.key === "flipped_to_fail" && flippedToFail.length > 0;
          return (
            <ChurnCell
              key={entry.key}
              entry={entry}
              {...(drillable ? { onActivate: openFlipDetails } : {})}
            />
          );
        })}
      </div>

      {twoWay && (
        <p className="hint">
          有样本变好也有样本变差 —— 均值可能持平但实际引入了新的失败模式，
          建议逐样本查看而非只看均值。
        </p>
      )}

      {flippedToFail.length > 0 && (
        <details
          className="flip-details"
          open={flipOpen}
          onToggle={(e) => setFlipOpen(e.currentTarget.open)}
        >
          <summary ref={flipSummaryRef}>
            转为失败的样本（{flippedToFail.length}）
          </summary>
          <div className="flip-actions">
            <button type="button" className="btn btn-sm" onClick={copyFlipped}>
              <Copy size={12} aria-hidden />
              复制全部 ID
            </button>
          </div>
          <ul className="mono flip-list">
            {flippedToFail.slice(0, FLIP_LIST_LIMIT).map((itemId) => (
              <li key={itemId}>{itemId}</li>
            ))}
          </ul>
          {flippedToFail.length > FLIP_LIST_LIMIT && (
            <p className="hint">
              仅显示前 {FLIP_LIST_LIMIT} 个，其余用上面的「复制全部 ID」取走。
            </p>
          )}
        </details>
      )}
    </div>
  );
}

const ChurnCell = memo(function ChurnCell({
  entry,
  onActivate,
}: {
  entry: ChurnEntry;
  onActivate?: () => void;
}) {
  const meta = TONE_META[entry.tone];
  // 0 不着色：把"没有样本变差"标成红色会让人误判为出了问题
  const valueCls = entry.value === 0 ? "stat-value" : `stat-value ${meta.cls}`;
  // 子集卡片降一级：视觉上不与大类平级，才不会被读成可相加的第五、六类
  const cls = entry.subset ? "stat-card churn-cell churn-cell-sub" : "stat-card churn-cell";

  const body = (
    <>
      <span className="stat-label">
        <meta.Icon size={12} aria-hidden />
        {entry.label}
        {!entry.subset && <span className="churn-tone-tag">{meta.label}</span>}
      </span>
      <span className={valueCls}>{entry.value}</span>
    </>
  );

  if (!onActivate) {
    return <div className={cls}>{body}</div>;
  }

  return (
    <button
      type="button"
      className={`${cls} churn-cell-link`}
      onClick={onActivate}
      aria-label={`${entry.label} ${entry.value} 个，查看样本明细`}
    >
      {body}
      <span className="churn-drill">
        查看明细
        <ChevronRight size={12} aria-hidden />
      </span>
    </button>
  );
});
