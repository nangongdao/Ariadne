import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api } from "@/api/client";
import type { SpanNode } from "@/api/types";
import { TableSkeleton } from "@/components/Skeleton";
import { SpanDetail } from "@/components/SpanDetail";
import { Timeline } from "@/components/Timeline";
import { TraceTree } from "@/components/TraceTree";
import { formatCost, formatDuration, formatTokens } from "@/lib/format";
import { collapseBeyondDepth } from "@/lib/tree";
import { ErrorBox } from "@/pages/TraceListPage";

type View = "tree" | "timeline";

/** 默认展开两层，更深的初始折叠 —— 一屏铺不下反而看不清结构 */
const DEFAULT_EXPAND_DEPTH = 2;

export function TraceDetailPage() {
  const { traceId = "" } = useParams<{ traceId: string }>();
  const [selected, setSelected] = useState<SpanNode | null>(null);
  const [view, setView] = useState<View>("tree");
  // 折叠与「只看失败」状态放在页面而非 TraceTree 内部：切换视图会把组件
  // 卸载，状态不提升的话用户展开的层级、筛选会在每次切换后全部重置
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [onlyErrors, setOnlyErrors] = useState(false);

  const { data, isPending, error } = useQuery({
    queryKey: ["trace", traceId],
    queryFn: () => api.getTrace(traceId),
    enabled: Boolean(traceId),
  });

  // 数据就绪时用默认深度播种一次折叠集。放在 effect 而非 useState 初始化，
  // 是因为 roots 来自异步查询。按 traceId 记忆：同一 trace 内视图来回切换
  // （data 引用不变）不重置用户展开的层级；切到另一个 trace 才重新播种
  const seededForRef = useRef<string | null>(null);
  useEffect(() => {
    if (!data || seededForRef.current === traceId) return;
    seededForRef.current = traceId;
    setCollapsed(collapseBeyondDepth(data.roots, DEFAULT_EXPAND_DEPTH));
    setOnlyErrors(false);
  }, [data, traceId]);

  if (isPending)
    return (
      <div className="page">
        <div className="skeleton" style={{ height: 28, width: 420, marginBottom: 14 }} />
        <div className="summary-stats" style={{ marginBottom: 16 }}>
          {Array.from({ length: 4 }, (_, i) => (
            <div key={i} className="skeleton" style={{ height: 52 }} />
          ))}
        </div>
        <TableSkeleton cols={3} rows={8} />
      </div>
    );
  if (error) {
    return (
      <div className="page">
        <ErrorBox error={error} />
        <Link to="/traces">← 返回列表</Link>
      </div>
    );
  }
  if (!data) return null;

  return (
    <div className="page trace-detail-page">
      <header className="page-header">
        <div>
          <Link to="/traces" className="back-link">
            ← Trace 列表
          </Link>
          <h1 className="mono trace-title" title={data.trace_id}>
            {data.trace_id}
          </h1>
        </div>
        <dl className="summary-stats">
          <div>
            <dt>Span</dt>
            <dd>{data.span_count}</dd>
          </div>
          <div>
            <dt>耗时</dt>
            <dd>{formatDuration(data.duration_ms)}</dd>
          </div>
          <div>
            <dt>Token</dt>
            <dd>{formatTokens(data.total_tokens)}</dd>
          </div>
          <div>
            <dt>成本</dt>
            <dd className="cost-value">{formatCost(data.total_cost_usd)}</dd>
          </div>
        </dl>
      </header>

      {data.truncated && (
        <div className="warn-box" role="alert">
          该 trace 的 span 数超过 5000，已截断展示。
        </div>
      )}

      <div className="view-switch" role="tablist" aria-label="视图切换">
        <button
          type="button"
          role="tab"
          aria-selected={view === "tree"}
          className={view === "tree" ? "active" : ""}
          onClick={() => setView("tree")}
        >
          调用树
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={view === "timeline"}
          className={view === "timeline" ? "active" : ""}
          onClick={() => setView("timeline")}
        >
          时间轴
        </button>
      </div>

      <div className="detail-layout">
        <div className="detail-main">
          {view === "tree" ? (
            <TraceTree
              roots={data.roots}
              totalMs={data.duration_ms}
              selectedId={selected?.span_id ?? null}
              onSelect={setSelected}
              collapsed={collapsed}
              setCollapsed={setCollapsed}
              onlyErrors={onlyErrors}
              setOnlyErrors={setOnlyErrors}
            />
          ) : (
            <Timeline
              roots={data.roots}
              totalMs={data.duration_ms}
              selectedId={selected?.span_id ?? null}
              onSelect={setSelected}
            />
          )}
        </div>
        <SpanDetail
          span={selected}
          traceId={data.trace_id}
          onClose={() => setSelected(null)}
        />
      </div>
    </div>
  );
}
