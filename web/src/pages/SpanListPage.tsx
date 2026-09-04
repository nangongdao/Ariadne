/**
 * Span 列表页 —— 跨 trace 的扁平检索。
 *
 * 筛选状态放在 URL 而非组件 state：成本页要能直接链过来（?model=…），
 * 地址要可分享，后退键要能退回上一组条件。
 *
 * 后端不支持翻页（只有 limit，上限 500），所以这里给的是条数选择器
 * 加一条截断提示，不假装有分页。
 */

import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { type MouseEvent, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { api } from "@/api/client";
import type { SpanFilters, SpanKind, SpanStatus } from "@/api/types";
import { KindBadge } from "@/components/KindBadge";
import { TableSkeleton } from "@/components/Skeleton";
import {
  DEFAULT_LIMIT,
  FACET_SAMPLE,
  type FilterPatch,
  KINDS,
  LIMITS,
  SEARCH_MAX,
  SEARCH_MIN,
  SpanFilterBar,
  STATUSES,
} from "@/components/spans/SpanFilterBar";
import { StatusDot } from "@/components/StatusDot";
import {
  formatAbsoluteTime,
  formatCost,
  formatDuration,
  formatRelativeTime,
  formatTokens,
  shortId,
} from "@/lib/format";
import { formatWindowLabel, MAX_WINDOW_HOURS } from "@/lib/time-window";
import { ErrorBox } from "@/pages/TraceListPage";

/**
 * 从 URL 还原筛选条件。
 *
 * URL 是用户可编辑的输入边界，枚举值必须校验白名单后再进请求，
 * 否则手改一个 ?kind=xxx 就直接换回一个 422。
 */
function parseFilters(params: URLSearchParams): SpanFilters {
  const filters: SpanFilters = {};

  const kind = params.get("kind");
  if (kind && (KINDS as string[]).includes(kind)) filters.kind = kind as SpanKind;

  const status = params.get("status");
  if (status && (STATUSES as string[]).includes(status)) {
    filters.status = status as SpanStatus;
  }

  const provider = params.get("provider");
  if (provider) filters.provider = provider;

  const model = params.get("model");
  if (model) filters.model = model;

  // 后端是 int（Query ge=0），小数直接 422
  const minDuration = Number(params.get("min_duration_ms"));
  if (Number.isFinite(minDuration) && minDuration > 0) {
    filters.min_duration_ms = Math.floor(minDuration);
  }

  // 成本页下钻带过来的时间窗。后端 le=24*90，越界必须在这里夹住而不是发出去换个 422
  const sinceHours = Number(params.get("since_hours"));
  if (Number.isFinite(sinceHours) && sinceHours >= 1) {
    filters.since_hours = Math.min(Math.floor(sinceHours), MAX_WINDOW_HOURS);
  }

  // 后端 search 限定 2–200 字符，越界发过去是必然的 422
  const search = params.get("search");
  if (search && search.length >= SEARCH_MIN && search.length <= SEARCH_MAX) {
    filters.search = search;
  }

  const limit = Number(params.get("limit"));
  filters.limit = LIMITS.includes(limit) ? limit : DEFAULT_LIMIT;

  return filters;
}

export function SpanListPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = useMemo(() => parseFilters(searchParams), [searchParams]);
  const [searchDraft, setSearchDraft] = useState(() => searchParams.get("search") ?? "");

  const { data, isPending, isFetching, error } = useQuery({
    queryKey: ["spans", filters],
    queryFn: () => api.listSpans(filters),
  });

  // provider/model 在后端是等值匹配，手输打错一个字就静默返回空。
  // 候选值必须来自数据本身，且这一查不带任何筛选 —— 否则选定某个模型后，
  // 候选表会收缩成它自己，再也切不到别的值。
  const facetsQuery = useQuery({
    queryKey: ["spans", "facets"],
    queryFn: () => api.listSpans({ limit: FACET_SAMPLE }),
    staleTime: 5 * 60_000,
  });

  // 取满 500 说明还有更早的数据没进候选表。此时下拉里「找不到某个模型」是采样
  // 造成的，不代表它不存在 —— 不说清楚，用户会以为自己记错了模型名
  const facetsCapped = (facetsQuery.data?.length ?? 0) >= FACET_SAMPLE;

  // 当前生效值必须并进候选表。成本页可以按 30 天统计并链过来，那个模型未必
  // 还在最近 500 条里；select 的 value 不在 options 中会静默显示成别的选项，
  // 于是"筛选真的生效了"和"界面显示的筛选"就不是一回事了。
  const { providers, models } = useMemo(() => {
    const rows = facetsQuery.data ?? [];
    const collect = (values: string[], active: string | undefined) =>
      [...new Set([...values, ...(active ? [active] : [])].filter(Boolean))].sort();
    return {
      providers: collect(
        rows.map((s) => s.provider),
        filters.provider,
      ),
      models: collect(
        rows.map((s) => s.model_request),
        filters.model,
      ),
    };
  }, [facetsQuery.data, filters.provider, filters.model]);

  const spans = data ?? [];
  const limit = filters.limit ?? DEFAULT_LIMIT;
  // 拿满一页说明后端还有货被这个 limit 截掉了，不说就等于谎报"就这些"
  const truncated = spans.length === limit;
  // limit 是常驻项，不算"筛选中"，否则重置按钮永远是可点的
  const hasActiveFilter = Object.keys(filters).some((k) => k !== "limit");

  const patch = (update: FilterPatch) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const [key, value] of Object.entries(update)) {
          // 空值删掉而非留空串，否则后端会当成有效过滤条件
          if (value === "" || value === undefined) next.delete(key);
          else next.set(key, String(value));
        }
        return next;
      },
      // 调筛选是修正当前视图而非新导航，replace 才不会把后退键塞满
      { replace: true },
    );
  };

  const clearFilters = () => {
    setSearchDraft("");
    setSearchParams(new URLSearchParams(), { replace: true });
  };

  /** 整行可点。中键/Ctrl 与行内链接交给浏览器，别抢新标签页 */
  const openTrace = (traceId: string) => (event: MouseEvent<HTMLTableRowElement>) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey) return;
    if ((event.target as HTMLElement).closest("a")) return;
    navigate(`/traces/${traceId}`);
  };

  return (
    <div className="page">
      <header className="page-header">
        <h1>Span</h1>
      </header>

      <SpanFilterBar
        filters={filters}
        providers={providers}
        models={models}
        facetsCapped={facetsCapped}
        searchDraft={searchDraft}
        isFetching={isFetching}
        hasActiveFilter={hasActiveFilter}
        onSearchDraftChange={setSearchDraft}
        onPatch={patch}
        onClear={clearFilters}
        onSubmit={() => {
          // 搜索走提交而非 onChange：每次按键都查会打爆后端。
          // 这里的下限与 parseFilters 必须一致，否则地址栏会显示一个没生效的搜索词
          const q = searchDraft.trim();
          patch({ search: q.length >= SEARCH_MIN ? q.slice(0, SEARCH_MAX) : undefined });
        }}
      />

      {error && <ErrorBox error={error} />}

      {isPending ? (
        <TableSkeleton cols={9} rows={12} />
      ) : spans.length === 0 ? (
        <EmptyResult hasFilter={hasActiveFilter} onClear={clearFilters} />
      ) : (
        <table className="data-table rows-link">
          <thead>
            <tr>
              <th scope="col">状态</th>
              <th scope="col">类型</th>
              <th scope="col">名称</th>
              <th scope="col">模型</th>
              <th scope="col">开始</th>
              <th scope="col" className="num">
                耗时
              </th>
              <th scope="col" className="num">
                Token
              </th>
              <th scope="col" className="num">
                成本
              </th>
              <th scope="col">Trace</th>
            </tr>
          </thead>
          <tbody>
            {spans.map((span) => (
              <tr key={span.span_id} onClick={openTrace(span.trace_id)}>
                <td>
                  <StatusDot status={span.status} />
                </td>
                <td>
                  <KindBadge kind={span.kind} />
                </td>
                <td title={span.name}>{span.name}</td>
                <td className="mono">{span.model_request || "—"}</td>
                <td title={formatAbsoluteTime(span.started_at)}>
                  {formatRelativeTime(span.started_at)}
                </td>
                <td className="num">{formatDuration(span.duration_ms)}</td>
                <td className="num">{formatTokens(span.total_tokens)}</td>
                <td className="num cost-value">{formatCost(span.cost_usd)}</td>
                <td>
                  <Link
                    to={`/traces/${span.trace_id}`}
                    className="mono link-strong"
                    title={span.trace_id}
                  >
                    {shortId(span.trace_id, 8)}
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {truncated && (
        <p className="hint truncate-note">
          正好取满 {limit} 条，可能还有更多被截断。结果按时间倒序，所以这是
          {filters.since_hours
            ? `最近 ${formatWindowLabel(filters.since_hours)}内最新的 ${limit} 条，不是最贵的 ${limit} 条`
            : `最新的 ${limit} 条`}
          。后端不支持翻页
          {limit < 500
            ? "，请调大「返回条数」（上限 500）。"
            : "，已到上限 500，请再收紧筛选条件。"}
        </p>
      )}
    </div>
  );
}

/** 空结果要能自救：有筛选就给清除入口，没筛选说明是真没数据 */
function EmptyResult({
  hasFilter,
  onClear,
}: {
  hasFilter: boolean;
  onClear: () => void;
}) {
  if (!hasFilter) {
    return <p className="hint">还没有采集到任何 span。接入 SDK 后发起一次请求即可。</p>;
  }
  return (
    <div className="empty-state">
      <p className="empty-state-title">没有匹配当前筛选的 span</p>
      <p className="hint">Provider 与模型是精确匹配，条件叠太多容易一条都不剩。</p>
      <button type="button" onClick={onClear}>
        <X size={13} aria-hidden />
        清除全部筛选
      </button>
    </div>
  );
}
