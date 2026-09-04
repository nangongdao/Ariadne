/**
 * 时间轴（甘特）视图。
 *
 * 存在的理由：调用树能看出层级但看不出时序重叠。同样是"父耗时 2s"，
 * 三个子调用串行跑完 vs 并发跑完是完全不同的优化方向，只有横向时间轴能区分。
 *
 * 不用 ECharts：甘特需要的自定义交互（点选、悬停联动、深层嵌套缩进）
 * 用 div + CSS 定位更直接，也避免为一个视图引入图表库的渲染开销。
 *
 * 只虚拟化纵轴：横向是相对总耗时的百分比，与滚动无关。
 */

import { useVirtualizer } from "@tanstack/react-virtual";
import { memo, useCallback, useMemo, useRef } from "react";

import type { SpanNode } from "@/api/types";
import { StatusDot } from "@/components/StatusDot";
import { formatDuration, kindLabel } from "@/lib/format";

const ROW_HEIGHT = 27;
/**
 * 刻度尺吸顶，行容器因此在滚动坐标里下移这么多。
 * 虚拟化的 scrollMargin 必须等于它，否则可视区间算错、长 trace 末尾会缺行。
 */
const AXIS_HEIGHT = 24;

interface Props {
  roots: SpanNode[];
  totalMs: number;
  selectedId: string | null;
  onSelect: (span: SpanNode) => void;
}

interface Bar {
  node: SpanNode;
  depth: number;
  /** 相对 trace 起点的偏移比例 0-1 */
  offsetRatio: number;
  widthRatio: number;
}

/** 极短的 span 也要能看见，给个最小宽度 */
const MIN_WIDTH_RATIO = 0.004;

function collectBars(roots: SpanNode[], traceStart: number, totalMs: number): Bar[] {
  const bars: Bar[] = [];
  const stack: Array<{ node: SpanNode; depth: number }> = [];

  for (let i = roots.length - 1; i >= 0; i -= 1) {
    const node = roots[i];
    if (node) stack.push({ node, depth: 0 });
  }

  while (stack.length > 0) {
    const item = stack.pop();
    if (!item) break;
    const { node, depth } = item;

    const start = new Date(node.started_at).getTime();
    const offset = Number.isNaN(start) ? 0 : start - traceStart;

    bars.push({
      node,
      depth,
      offsetRatio: totalMs > 0 ? Math.max(offset / totalMs, 0) : 0,
      widthRatio:
        totalMs > 0 ? Math.max(node.duration_ms / totalMs, MIN_WIDTH_RATIO) : 1,
    });

    for (let i = node.children.length - 1; i >= 0; i -= 1) {
      const child = node.children[i];
      if (child) stack.push({ node: child, depth: depth + 1 });
    }
  }

  return bars;
}

export function Timeline({ roots, totalMs, selectedId, onSelect }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);

  const bars = useMemo(() => {
    const starts = collectStarts(roots);
    const traceStart = starts.length > 0 ? Math.min(...starts) : 0;
    return collectBars(roots, traceStart, totalMs);
  }, [roots, totalMs]);

  const ticks = useMemo(() => buildTicks(totalMs), [totalMs]);

  const virtualizer = useVirtualizer({
    count: bars.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
    scrollMargin: AXIS_HEIGHT,
  });

  return (
    <div className="timeline">
      <div className="timeline-scroll" ref={scrollRef}>
        <div
          className="timeline-axis"
          style={{ height: AXIS_HEIGHT }}
          aria-hidden="true"
        >
          <div className="axis-scale">
            {ticks.map((tick) => (
              <span
                key={tick.ratio}
                className="axis-tick"
                style={{ left: `${tick.ratio * 100}%` }}
              >
                {tick.label}
              </span>
            ))}
          </div>
        </div>

        <div
          role="list"
          style={{ height: virtualizer.getTotalSize(), position: "relative" }}
        >
          {virtualizer.getVirtualItems().map((virtualRow) => {
            const bar = bars[virtualRow.index];
            if (!bar) return null;
            return (
              <TimelineRow
                key={bar.node.span_id}
                bar={bar}
                totalMs={totalMs}
                top={virtualRow.start - AXIS_HEIGHT}
                selected={selectedId === bar.node.span_id}
                onSelect={onSelect}
              />
            );
          })}
        </div>
      </div>
    </div>
  );
}

interface RowProps {
  bar: Bar;
  totalMs: number;
  top: number;
  selected: boolean;
  onSelect: (span: SpanNode) => void;
}

const TimelineRow = memo(function TimelineRow({
  bar,
  totalMs,
  top,
  selected,
  onSelect,
}: RowProps) {
  const { node, depth, offsetRatio, widthRatio } = bar;
  const handleSelect = useCallback(() => onSelect(node), [onSelect, node]);
  // 偏移 + 宽度不能溢出容器
  const clampedWidth = Math.min(widthRatio, 1 - offsetRatio);

  return (
    <div
      className={[
        "timeline-row",
        selected ? "selected" : "",
        node.status !== "ok" ? "row-error" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      role="listitem"
      style={{ position: "absolute", top, height: ROW_HEIGHT, left: 0, right: 0 }}
    >
      <button
        type="button"
        className="timeline-label"
        style={{ paddingLeft: depth * 12 + 8 }}
        onClick={handleSelect}
        title={`${kindLabel(node.kind)} · ${node.name}`}
      >
        <StatusDot status={node.status} />
        <span className="label-text">{node.name}</span>
      </button>

      <div className="timeline-track">
        <button
          type="button"
          className={`timeline-bar bar-${node.kind}`}
          style={{
            left: `${offsetRatio * 100}%`,
            width: `${clampedWidth * 100}%`,
          }}
          onClick={handleSelect}
          aria-label={`${node.name}，耗时 ${formatDuration(node.duration_ms)}`}
          title={`${node.name}\n开始偏移 ${formatDuration(offsetRatio * totalMs)}\n耗时 ${formatDuration(node.duration_ms)}\n自身 ${formatDuration(node.self_ms)}`}
        >
          <span className="bar-duration">{formatDuration(node.duration_ms)}</span>
        </button>
      </div>
    </div>
  );
});

function collectStarts(roots: SpanNode[]): number[] {
  const starts: number[] = [];
  const stack = [...roots];
  while (stack.length > 0) {
    const node = stack.pop();
    if (!node) break;
    const time = new Date(node.started_at).getTime();
    if (!Number.isNaN(time)) starts.push(time);
    stack.push(...node.children);
  }
  return starts;
}

function buildTicks(totalMs: number): Array<{ ratio: number; label: string }> {
  const count = 5;
  return Array.from({ length: count + 1 }, (_, index) => {
    const ratio = index / count;
    return { ratio, label: formatDuration(totalMs * ratio) };
  });
}
