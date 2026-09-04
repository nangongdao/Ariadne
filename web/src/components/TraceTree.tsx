/**
 * Trace 调用树。
 *
 * 自研而非用现成树组件的理由（见 docs/07）：需要"嵌套 + 折叠 + 多维筛选
 * + 关键路径高亮 + 瓶颈标注"的组合，现成库都只满足其中一部分。
 *
 * 虚拟滚动：单 trace 可达数千 span，全量渲染会卡死。
 * 键盘导航：漫游 tabindex，整棵树只占一个 Tab 停留点。
 */

import { useVirtualizer } from "@tanstack/react-virtual";
import { ChevronRight, CircleDollarSign, TriangleAlert } from "lucide-react";
import type { FocusEvent, KeyboardEvent, MouseEvent } from "react";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { SpanNode } from "@/api/types";
import { KindBadge } from "@/components/KindBadge";
import { StatusDot } from "@/components/StatusDot";
import { formatCost, formatDuration, formatTokens, shortId } from "@/lib/format";
import type { FlatRow } from "@/lib/tree";
import {
  collectIds,
  findBottlenecks,
  findCriticalPath,
  flattenErrors,
  flattenVisible,
  totalTokens,
} from "@/lib/tree";

const ROW_HEIGHT = 34;
const INDENT_PX = 18;

interface Props {
  roots: SpanNode[];
  totalMs: number;
  selectedId: string | null;
  onSelect: (span: SpanNode) => void;
  /** 折叠集与「只看失败」由父组件持有：调用树⇄时间轴切换时
      用户展开的层级和筛选不该被 reset（卸载即丢） */
  collapsed: Set<string>;
  setCollapsed: (updater: (prev: Set<string>) => Set<string>) => void;
  onlyErrors: boolean;
  setOnlyErrors: (next: boolean) => void;
}

export function TraceTree({
  roots,
  totalMs,
  selectedId,
  onSelect,
  collapsed,
  setCollapsed,
  onlyErrors,
  setOnlyErrors,
}: Props) {
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  /** 目标行不在渲染窗口内时，先滚动，等它挂载后再由 effect 补上 focus */
  const pendingFocusRef = useRef<string | null>(null);

  const criticalPath = useMemo(() => findCriticalPath(roots), [roots]);
  const bottlenecks = useMemo(() => findBottlenecks(roots, totalMs), [roots, totalMs]);

  // 始终算：勾选框旁要显示总数，否则用户不勾就不知道树里到底有没有失败，
  // 而这正是列表页「失败 N」与树对不上的地方
  const errorRows = useMemo(() => flattenErrors(roots, criticalPath), [roots, criticalPath]);
  const errorCount = errorRows.length;

  const rows = useMemo(
    () => (onlyErrors ? errorRows : flattenVisible(roots, collapsed, criticalPath)),
    [onlyErrors, errorRows, roots, collapsed, criticalPath],
  );

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
  });

  // 焦点行被筛掉或折叠后，漫游落到首行，保证树始终有且只有一个 Tab 停留点
  const activeIndex = useMemo(() => {
    const found = rows.findIndex((row) => row.node.span_id === focusedId);
    return found >= 0 ? found : 0;
  }, [rows, focusedId]);
  const activeId = rows[activeIndex]?.node.span_id ?? null;

  const focusRow = useCallback(
    (index: number) => {
      const row = rows[index];
      if (!row) return;
      const spanId = row.node.span_id;
      setFocusedId(spanId);
      virtualizer.scrollToIndex(index, { align: "auto" });

      const el = findRowElement(scrollRef.current, spanId);
      if (el) {
        pendingFocusRef.current = null;
        el.focus({ preventScroll: true });
      } else {
        pendingFocusRef.current = spanId;
      }
    },
    [rows, virtualizer],
  );

  // 行集合变化说明这次 pending 已过期，丢弃以免稍后抢焦点
  useEffect(() => {
    pendingFocusRef.current = null;
  }, [rows]);

  useEffect(() => {
    const spanId = pendingFocusRef.current;
    if (!spanId) return;
    const el = findRowElement(scrollRef.current, spanId);
    if (!el) return;
    pendingFocusRef.current = null;
    el.focus({ preventScroll: true });
  });

  const toggle = useCallback((spanId: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(spanId)) next.delete(spanId);
      else next.add(spanId);
      return next;
    });
  }, [setCollapsed]);

  const expandAll = useCallback(() => setCollapsed(() => new Set()), [setCollapsed]);
  const collapseAll = useCallback(
    () => setCollapsed(() => new Set(collectIds(roots).filter((id) => id))),
    [roots, setCollapsed],
  );

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      const row = rows[activeIndex];
      if (!row) return;
      const { node, hasChildren } = row;
      const isCollapsed = collapsed.has(node.span_id);

      switch (event.key) {
        case "ArrowDown":
          event.preventDefault();
          focusRow(Math.min(activeIndex + 1, rows.length - 1));
          break;
        case "ArrowUp":
          event.preventDefault();
          focusRow(Math.max(activeIndex - 1, 0));
          break;
        case "ArrowRight":
          event.preventDefault();
          if (!hasChildren) break;
          if (isCollapsed) toggle(node.span_id);
          else focusRow(activeIndex + 1);
          break;
        case "ArrowLeft": {
          event.preventDefault();
          if (hasChildren && !isCollapsed) {
            toggle(node.span_id);
            break;
          }
          const parent = findParentIndex(rows, activeIndex);
          if (parent >= 0) focusRow(parent);
          break;
        }
        case "Home":
          event.preventDefault();
          focusRow(0);
          break;
        case "End":
          event.preventDefault();
          focusRow(rows.length - 1);
          break;
        case "Enter":
        case " ":
          event.preventDefault();
          onSelect(node);
          break;
        default:
          break;
      }
    },
    [rows, activeIndex, collapsed, focusRow, toggle, onSelect],
  );

  // 鼠标点选后漫游锚点要跟上，否则接着按方向键会从旧位置跳走
  const handleFocusCapture = useCallback((event: FocusEvent<HTMLDivElement>) => {
    const host = event.target.closest<HTMLElement>("[data-span-id]");
    const spanId = host?.dataset.spanId;
    if (spanId) setFocusedId(spanId);
  }, []);

  return (
    <div className="trace-tree">
      <div className="tree-toolbar">
        {/* 筛选态是全树平铺，没有层级可展开 —— 按钮留着能点但没反应更费解 */}
        <button type="button" onClick={expandAll} disabled={onlyErrors}>
          全部展开
        </button>
        <button type="button" onClick={collapseAll} disabled={onlyErrors}>
          全部折叠
        </button>
        <label className="toolbar-check">
          <input
            type="checkbox"
            checked={onlyErrors}
            onChange={(event) => setOnlyErrors(event.target.checked)}
          />
          只看失败
          {errorCount > 0 && <span className="toolbar-count">{errorCount}</span>}
        </label>
        <span className="toolbar-hint">
          {onlyErrors
            ? `${rows.length} 个失败 span（全树，含已折叠层级）`
            : `${rows.length} 行 · 方向键导航 · 加粗为关键路径 · 标记 = 慢 / 高成本`}
        </span>
      </div>

      <div className="tree-scroll" ref={scrollRef}>
        <div
          role="tree"
          aria-label="调用树"
          aria-multiselectable={false}
          style={{ height: virtualizer.getTotalSize(), position: "relative" }}
          onKeyDown={handleKeyDown}
          onFocusCapture={handleFocusCapture}
        >
          {virtualizer.getVirtualItems().map((virtualRow) => {
            const row = rows[virtualRow.index];
            if (!row) return null;
            const { node } = row;
            return (
              <TreeRow
                key={node.span_id}
                row={row}
                totalMs={totalMs}
                top={virtualRow.start}
                collapsed={collapsed.has(node.span_id)}
                selected={selectedId === node.span_id}
                active={activeId === node.span_id}
                slow={bottlenecks.slow.has(node.span_id)}
                expensive={bottlenecks.expensive.has(node.span_id)}
                onSelect={onSelect}
                onToggle={toggle}
              />
            );
          })}
        </div>
      </div>
    </div>
  );
}

/** 同一深度往上找最近的浅一层节点，即视觉上的父行。 */
function findParentIndex(rows: FlatRow[], index: number): number {
  const current = rows[index];
  if (!current || current.depth === 0) return -1;
  for (let i = index - 1; i >= 0; i -= 1) {
    const candidate = rows[i];
    if (candidate && candidate.depth < current.depth) return i;
  }
  return -1;
}

function findRowElement(scroller: HTMLElement | null, spanId: string) {
  return (
    scroller?.querySelector<HTMLElement>(`[data-span-id="${CSS.escape(spanId)}"]`) ?? null
  );
}

/** 折叠三角不参与 Tab 顺序，点它也不该把焦点从行上抢走 */
function keepFocusOnRow(event: MouseEvent<HTMLButtonElement>) {
  event.preventDefault();
}

interface RowProps {
  row: FlatRow;
  totalMs: number;
  top: number;
  collapsed: boolean;
  selected: boolean;
  active: boolean;
  slow: boolean;
  expensive: boolean;
  onSelect: (span: SpanNode) => void;
  onToggle: (spanId: string) => void;
}

const TreeRow = memo(function TreeRow({
  row,
  totalMs,
  top,
  collapsed,
  selected,
  active,
  slow,
  expensive,
  onSelect,
  onToggle,
}: RowProps) {
  const { node, depth, hasChildren, onCriticalPath } = row;
  const rowRef = useRef<HTMLDivElement>(null);
  const tokens = totalTokens(node);
  const cost = Number.parseFloat(node.cost_usd) || 0;
  const selfRatio = totalMs > 0 ? node.self_ms / totalMs : 0;

  const handleClick = useCallback(() => onSelect(node), [onSelect, node]);

  const handleToggle = useCallback(
    (event: MouseEvent<HTMLButtonElement>) => {
      event.stopPropagation();
      onToggle(node.span_id);
      rowRef.current?.focus({ preventScroll: true });
    },
    [onToggle, node.span_id],
  );

  return (
    <div
      ref={rowRef}
      data-span-id={node.span_id}
      className={[
        "tree-row",
        onCriticalPath ? "critical" : "",
        selected ? "selected" : "",
        node.status !== "ok" ? "row-error" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      style={{ position: "absolute", top, height: ROW_HEIGHT, left: 0, right: 0 }}
      onClick={handleClick}
      role="treeitem"
      tabIndex={active ? 0 : -1}
      aria-level={depth + 1}
      aria-selected={selected}
      {...(hasChildren ? { "aria-expanded": !collapsed } : {})}
    >
      <span className="tree-indent" style={{ width: depth * INDENT_PX }} />

      {hasChildren ? (
        <button
          type="button"
          className="tree-toggle"
          data-expanded={collapsed ? "false" : "true"}
          tabIndex={-1}
          aria-hidden="true"
          title={collapsed ? "展开" : "折叠"}
          onMouseDown={keepFocusOnRow}
          onClick={handleToggle}
        >
          <ChevronRight size={13} />
        </button>
      ) : (
        <span className="tree-toggle-placeholder" />
      )}

      <StatusDot status={node.status} />
      <KindBadge kind={node.kind} />

      <span className="tree-name" title={node.name}>
        {node.name}
      </span>

      {node.model_request && (
        <span className="tree-model" title={node.model_response || undefined}>
          {node.model_request}
        </span>
      )}

      <span className="tree-metrics">
        {slow && (
          <span
            className="flag flag-slow"
            title={`自身耗时占比 ${(selfRatio * 100).toFixed(0)}%`}
          >
            <TriangleAlert size={11} aria-label="慢" />
          </span>
        )}
        {expensive && (
          <span className="flag flag-cost" title="成本占比高">
            <CircleDollarSign size={11} aria-label="高成本" />
          </span>
        )}
        <span
          className="metric metric-time"
          title={`总耗时 ${formatDuration(node.duration_ms)}，自身 ${formatDuration(node.self_ms)}`}
        >
          {formatDuration(node.duration_ms)}
        </span>
        {tokens > 0 && <span className="metric metric-token">{formatTokens(tokens)}</span>}
        {cost > 0 && <span className="metric metric-cost">{formatCost(node.cost_usd)}</span>}
        <span className="metric metric-id" title={node.span_id}>
          {shortId(node.span_id, 6)}
        </span>
      </span>
    </div>
  );
});
