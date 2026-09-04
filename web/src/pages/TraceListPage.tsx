import { useQuery } from "@tanstack/react-query";
import { Activity, Check } from "lucide-react";
import { type MouseEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "@/api/client";
import { TableSkeleton } from "@/components/Skeleton";
import { StatusDot } from "@/components/StatusDot";
import {
  formatAbsoluteTime,
  formatCost,
  formatDuration,
  formatRelativeTime,
  formatTokens,
  shortId,
} from "@/lib/format";

const PAGE_SIZE = 50;
const REFRESH_MS = 5000;

export function TraceListPage() {
  const navigate = useNavigate();
  const [onlyErrors, setOnlyErrors] = useState(false);
  // 游标分页：用 started_at 而非 offset，深分页时 offset 会退化为全扫。
  // 存成栈而不是单个游标，否则只能一路往前翻，回退唯一出路是跳回最新一页。
  const [cursorStack, setCursorStack] = useState<string[]>([]);
  const cursor = cursorStack.at(-1);

  const { data, isPending, error, isFetching } = useQuery({
    queryKey: ["traces", { onlyErrors, cursor }],
    queryFn: () =>
      api.listTraces({
        limit: PAGE_SIZE,
        only_errors: onlyErrors,
        ...(cursor ? { before: cursor } : {}),
      }),
    // 首页轮询，翻页后停止（用户在看历史数据，不该被刷新打断）
    refetchInterval: cursor ? false : REFRESH_MS,
  });

  const traces = data ?? [];
  const canLoadMore = traces.length === PAGE_SIZE;
  const pageNumber = cursorStack.length + 1;

  /** 整行可点。中键/Ctrl 点击与行内链接自身交给浏览器，别抢新标签页的行为 */
  const openTrace = (traceId: string) => (event: MouseEvent<HTMLTableRowElement>) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey) return;
    if ((event.target as HTMLElement).closest("a")) return;
    navigate(`/traces/${traceId}`);
  };

  // 头部统计条：从已加载的页面数据即时汇总，无需额外请求
  const failedTraces = traces.reduce((sum, t) => sum + (t.error_count > 0 ? 1 : 0), 0);
  const pageTokens = traces.reduce((sum, t) => sum + t.total_tokens, 0);
  const pageCost = traces.reduce((sum, t) => sum + (Number(t.total_cost_usd) || 0), 0);

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>Trace</h1>
          <p className="page-subtitle">全部链路请求 · 每 5 秒自动刷新</p>
        </div>
        <div className="page-actions">
          {isFetching && <span className="hint">刷新中…</span>}
          {/* 用 button[aria-pressed] 而非 label+checkbox：选中态样式挂在
              [aria-pressed="true"] 上，label 永远匹配不到，按下去看不出变化 */}
          <button
            type="button"
            className="chip-toggle"
            aria-pressed={onlyErrors}
            onClick={() => {
              setOnlyErrors((v) => !v);
              setCursorStack([]);
            }}
          >
            {onlyErrors && <Check size={12} aria-hidden />}
            只看有失败的
          </button>
        </div>
      </header>

      {/* 作用域标在组上而不是每个 chip 前缀一遍"本页"：四个标签都加会很吵，
          但不加就会被当成全局统计 —— 副标题写着"全部链路请求"，更容易读错。
          全窗口口径在成本页，这里给出入口 */}
      <div className="stat-chips">
        <span className="stat-chips-scope">
          第 {pageNumber} 页合计
          <Link to="/costs" className="stat-chips-link">
            看全部
          </Link>
        </span>
        <div className="stat-chip">
          <span className="stat-chip-label">Trace</span>
          <span className="stat-chip-value">{traces.length}</span>
        </div>
        <div className={`stat-chip${failedTraces ? " has-error" : ""}`}>
          <span className="stat-chip-label">含失败</span>
          <span className="stat-chip-value">{failedTraces}</span>
        </div>
        <div className="stat-chip">
          <span className="stat-chip-label">Token</span>
          <span className="stat-chip-value">{formatTokens(pageTokens)}</span>
        </div>
        <div className="stat-chip">
          <span className="stat-chip-label">成本</span>
          <span className="stat-chip-value cost-value">{formatCost(pageCost)}</span>
        </div>
      </div>

      {error && <ErrorBox error={error} />}

      {isPending ? (
        <TableSkeleton cols={9} rows={10} />
      ) : traces.length === 0 ? (
        <EmptyState onlyErrors={onlyErrors} />
      ) : (
        <table className="data-table rows-link">
          <thead>
            <tr>
              <th scope="col">状态</th>
              <th scope="col">根节点</th>
              <th scope="col">Trace ID</th>
              <th scope="col">开始</th>
              <th scope="col" className="num">
                耗时
              </th>
              <th scope="col" className="num">
                Span
              </th>
              <th scope="col" className="num">
                Token
              </th>
              <th scope="col" className="num">
                成本
              </th>
              <th scope="col">模型</th>
            </tr>
          </thead>
          <tbody>
            {traces.map((trace) => (
              <tr key={trace.trace_id} onClick={openTrace(trace.trace_id)}>
                <td>
                  <StatusDot status={trace.error_count > 0 ? "error" : "ok"} />
                  {trace.error_count > 0 && (
                    <span className="error-count">{trace.error_count}</span>
                  )}
                </td>
                <td>
                  <Link to={`/traces/${trace.trace_id}`} className="link-strong">
                    {trace.root_name || "（无根节点）"}
                  </Link>
                </td>
                <td className="mono" title={trace.trace_id}>
                  {shortId(trace.trace_id, 12)}
                </td>
                <td title={formatAbsoluteTime(trace.started_at)}>
                  {formatRelativeTime(trace.started_at)}
                </td>
                <td className="num">{formatDuration(trace.duration_ms)}</td>
                <td className="num">{trace.span_count}</td>
                <td className="num">{formatTokens(trace.total_tokens)}</td>
                <td className="num cost-value">{formatCost(trace.total_cost_usd)}</td>
                <td className="cell-models">
                  {[...new Set(trace.models)].slice(0, 2).map((model) => (
                    <span key={model} className="tag tag-sm">
                      {model}
                    </span>
                  ))}
                  {[...new Set(trace.models)].length > 2 && (
                    <span className="hint">+{[...new Set(trace.models)].length - 2}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="pager">
        {cursor && (
          <>
            <button type="button" onClick={() => setCursorStack([])}>
              回到最新
            </button>
            <button type="button" onClick={() => setCursorStack((s) => s.slice(0, -1))}>
              ← 上一页
            </button>
          </>
        )}
        {canLoadMore && (
          <button
            type="button"
            onClick={() => {
              const last = traces.at(-1);
              if (last) setCursorStack((s) => [...s, last.started_at]);
            }}
          >
            更早的 →
          </button>
        )}
        {!isPending && <span className="hint">第 {pageNumber} 页 · {traces.length} 条</span>}
      </div>
    </div>
  );
}

function EmptyState({ onlyErrors }: { onlyErrors: boolean }) {
  if (onlyErrors) {
    return <p className="hint">没有包含失败 span 的 trace。</p>;
  }
  return (
    <div className="empty-state">
      <div className="empty-state-icon" aria-hidden>
        <Activity size={24} />
      </div>
      <p className="empty-state-title">还没有采集到任何 trace</p>
      <p className="hint">
        接入 SDK 后发起一个请求即可看到完整调用树，例如：
        <code>uv run python examples/demo_rag.py</code>
      </p>
    </div>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : String(error);
  const isAuth =
    typeof error === "object" &&
    error !== null &&
    "isAuthError" in error &&
    Boolean((error as { isAuthError: unknown }).isAuthError);

  // 后端 422 会在 problem 里带 reasons（逐条说明哪儿不合格）。
  // 只渲染 message 等于把这些原因扔掉，用户只看到一句"校验失败"。
  const problem =
    typeof error === "object" && error !== null && "problem" in error
      ? (error as { problem: Record<string, unknown> | null }).problem
      : null;
  const rawReasons = problem?.["reasons"];
  const reasons = Array.isArray(rawReasons) ? rawReasons.map(String) : [];

  return (
    <div className="error-box" role="alert">
      <strong>请求失败</strong>
      <p>{message}</p>
      {reasons.length > 0 && (
        <ul className="error-reasons">
          {reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      )}
      {isAuth && (
        <p className="hint">
          API Key 无效。到 <Link to="/settings">设置</Link> 填入正确的 key。
        </p>
      )}
    </div>
  );
}
