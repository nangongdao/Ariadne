/**
 * Span 列表的筛选栏。
 *
 * provider / model 是后端的等值匹配，所以这里一律用 select 而非文本框：
 * 手输打错一个字会静默返回空表，用户只会以为"没数据"。候选值由页面从
 * 数据本身汇总后传进来。
 */

import { Search, X } from "lucide-react";
import { memo, useId } from "react";

import type { SpanFilters, SpanKind, SpanStatus } from "@/api/types";
import { formatWindowLabel, TIME_WINDOWS } from "@/lib/time-window";

export const KINDS: SpanKind[] = [
  "llm",
  "tool",
  "rag",
  "code",
  "loop",
  "harness",
  "eval",
  "internal",
];
export const STATUSES: SpanStatus[] = ["ok", "error", "blocked"];

/** 后端 limit 上限 500（traces.py list_spans），超出会被拒 */
export const LIMITS = [50, 100, 200, 500];
export const DEFAULT_LIMIT = 100;

/**
 * 汇总候选值时取的样本量。就是后端上限 —— 没有 DISTINCT 接口，
 * 能扫多少算多少，所以候选表天然只覆盖最近这批数据。
 */
export const FACET_SAMPLE = 500;

/** 后端 search 的 min_length / max_length，越界会 422 */
export const SEARCH_MIN = 2;
export const SEARCH_MAX = 200;

/** 传 undefined 表示清除该条件，exactOptionalPropertyTypes 下需显式放开 */
export type FilterPatch = { [K in keyof SpanFilters]?: SpanFilters[K] | undefined };

interface Props {
  filters: SpanFilters;
  providers: string[];
  models: string[];
  /** 候选值取自最近 500 条 span 且已取满 —— 更早出现过的模型不在下拉里 */
  facetsCapped: boolean;
  searchDraft: string;
  isFetching: boolean;
  hasActiveFilter: boolean;
  onSearchDraftChange: (value: string) => void;
  onPatch: (update: FilterPatch) => void;
  onSubmit: () => void;
  onClear: () => void;
}

export const SpanFilterBar = memo(function SpanFilterBar({
  filters,
  providers,
  models,
  facetsCapped,
  searchDraft,
  isFetching,
  hasActiveFilter,
  onSearchDraftChange,
  onPatch,
  onSubmit,
  onClear,
}: Props) {
  const kindId = useId();
  const statusId = useId();
  const providerId = useId();
  const modelId = useId();
  const windowId = useId();
  const durationId = useId();
  const searchId = useId();
  const limitId = useId();

  const tooShort = searchDraft.trim().length > 0 && searchDraft.trim().length < SEARCH_MIN;

  // 下钻可能带来预设之外的小时数（成本页档位变了，或用户手改地址栏）。
  // 不补进 options 的话 select 会静默回落到第一项，显示的筛选就不是生效的筛选
  const activeWindow = filters.since_hours;
  const windowOptions =
    activeWindow && !TIME_WINDOWS.some((w) => w.hours === activeWindow)
      ? [...TIME_WINDOWS, { label: formatWindowLabel(activeWindow), hours: activeWindow }]
      : TIME_WINDOWS;

  return (
    <form
      className="filter-bar"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <div className="filter-field">
        <label htmlFor={kindId}>类型</label>
        <select
          id={kindId}
          value={filters.kind ?? ""}
          onChange={(event) =>
            onPatch({ kind: (event.target.value || undefined) as SpanKind | undefined })
          }
        >
          <option value="">全部</option>
          {KINDS.map((kind) => (
            <option key={kind} value={kind}>
              {kind}
            </option>
          ))}
        </select>
      </div>

      <div className="filter-field">
        <label htmlFor={statusId}>状态</label>
        <select
          id={statusId}
          value={filters.status ?? ""}
          onChange={(event) =>
            onPatch({ status: (event.target.value || undefined) as SpanStatus | undefined })
          }
        >
          <option value="">全部</option>
          {STATUSES.map((status) => (
            <option key={status} value={status}>
              {status}
            </option>
          ))}
        </select>
      </div>

      <div className="filter-field">
        <label htmlFor={windowId}>时间范围</label>
        <select
          id={windowId}
          value={activeWindow ?? ""}
          onChange={(event) =>
            onPatch({
              since_hours: event.target.value ? Number(event.target.value) : undefined,
            })
          }
        >
          <option value="">不限</option>
          {windowOptions.map((w) => (
            <option key={w.hours} value={w.hours}>
              最近 {w.label}
            </option>
          ))}
        </select>
      </div>

      <div className="filter-field">
        <label htmlFor={providerId}>Provider</label>
        <select
          id={providerId}
          value={filters.provider ?? ""}
          disabled={providers.length === 0}
          onChange={(event) => onPatch({ provider: event.target.value || undefined })}
        >
          <option value="">全部</option>
          {providers.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </div>

      <div className="filter-field">
        <label htmlFor={modelId}>模型</label>
        <select
          id={modelId}
          value={filters.model ?? ""}
          disabled={models.length === 0}
          onChange={(event) => onPatch({ model: event.target.value || undefined })}
        >
          <option value="">全部</option>
          {models.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
          {/* 只在模型这一栏说明采样范围：provider 只有三个可能值，取满 500 条
              几乎不可能漏掉，标上去纯属噪音。写成 disabled option 是为了让
              「找不到我要的模型」和解释出现在同一个位置 —— 放在下拉外面，
              正在翻列表的人看不到 */}
          {facetsCapped && (
            <option value="" disabled>
              —— 以上取自最近 {FACET_SAMPLE} 条 span ——
            </option>
          )}
        </select>
      </div>

      <div className="filter-field">
        <label htmlFor={durationId}>最小耗时(ms)</label>
        {/* 后端是 int，小数会 422。step=1 只管住上下箭头，直接键入 1.5 仍要在这里取整 */}
        <input
          id={durationId}
          type="number"
          min={0}
          step={1}
          value={filters.min_duration_ms ?? ""}
          onChange={(event) => {
            const raw = Number(event.target.value);
            onPatch({
              min_duration_ms:
                event.target.value && Number.isFinite(raw) && raw > 0
                  ? Math.floor(raw)
                  : undefined,
            });
          }}
        />
      </div>

      <div className="filter-field">
        <label htmlFor={limitId}>返回条数</label>
        <select
          id={limitId}
          value={filters.limit ?? DEFAULT_LIMIT}
          onChange={(event) => onPatch({ limit: Number(event.target.value) })}
        >
          {LIMITS.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
      </div>

      <div className="filter-field grow">
        <label htmlFor={searchId}>搜索</label>
        <input
          id={searchId}
          type="search"
          value={searchDraft}
          maxLength={SEARCH_MAX}
          onChange={(event) => onSearchDraftChange(event.target.value)}
          placeholder="名称 / 输入 / 输出（≥2 字符，回车搜索）"
          {...(tooShort ? { "aria-describedby": `${searchId}-hint` } : {})}
        />
        {tooShort && (
          <span id={`${searchId}-hint`} className="hint">
            至少 {SEARCH_MIN} 个字符
          </span>
        )}
      </div>

      <div className="filter-actions">
        <button type="submit" className="btn-primary" disabled={isFetching}>
          <Search size={13} aria-hidden />
          {isFetching ? "查询中…" : "查询"}
        </button>
        <button
          type="button"
          disabled={!hasActiveFilter && !searchDraft}
          onClick={onClear}
        >
          <X size={13} aria-hidden />
          重置
        </button>
      </div>
    </form>
  );
});
