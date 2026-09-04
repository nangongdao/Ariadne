/**
 * 成本归因页 —— 时间窗 + 分组维度下的花销分布。
 *
 * 按模型/Provider 分组时，每行可下钻到 Span 列表的同名筛选：
 * "哪个模型烧钱"和"具体是哪些调用在烧"本来是同一个问题的两半。
 */

import { useQuery } from "@tanstack/react-query";
import { ArrowRight } from "lucide-react";
import { useId, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "@/api/client";
import { Chart } from "@/components/ChartLazy";
import { CardGridSkeleton } from "@/components/Skeleton";
import { buildCostChartOption, isTimeDimension } from "@/lib/cost-chart";
import { formatCost, formatPercent, formatTokens } from "@/lib/format";
import { DEFAULT_WINDOW_HOURS, TIME_WINDOWS } from "@/lib/time-window";
import { ErrorBox } from "@/pages/TraceListPage";

const GROUPINGS = [
  { label: "按模型", value: "model_request" },
  { label: "按 Provider", value: "provider" },
  { label: "按小时", value: "hour" },
  { label: "按天", value: "day" },
] as const;

/**
 * 时间展开档位。后端 group_by 收逗号分隔的多维度，但这里只放开
 * 「一个归因维度 × 一个时间维度」这一种组合 ——
 * provider+model+minute 之类的任意组合会让桶数爆到 _MAX_BUCKETS(2400) 之外，
 * 图表和表格都读不出东西，等于给了个没法用的功能。
 */
const TIME_EXPANSIONS = [
  { label: "不展开", value: "" },
  { label: "按小时", value: "hour" },
  { label: "按天", value: "day" },
] as const;

/** 分组维度 → Span 列表的筛选参数名。时间维度没有对应筛选，故不可下钻 */
const DRILLDOWN_PARAM: Record<string, string> = {
  model_request: "model",
  provider: "provider",
};

export function CostPage() {
  const windowId = useId();
  const groupId = useId();
  const expandId = useId();
  const [hours, setHours] = useState<number>(DEFAULT_WINDOW_HOURS);
  const [groupBy, setGroupBy] = useState<string>("model_request");
  const [expandBy, setExpandBy] = useState<string>("");

  const isTimeSeries = isTimeDimension(groupBy);
  // 主维度已经是时间了就不能再按时间展开。这里就地压掉而不是清 state：
  // 用户从「按模型+按天」切到「按天」再切回来，展开档位应该还在
  const expansion = isTimeSeries ? "" : expandBy;
  const groupParam = expansion ? `${groupBy},${expansion}` : groupBy;

  const { data, isPending, error } = useQuery({
    queryKey: ["costs", { hours, groupParam }],
    queryFn: () => api.costs({ hours, group_by: groupParam }),
  });

  const drilldownParam = DRILLDOWN_PARAM[groupBy];
  const groupLabel = GROUPINGS.find((g) => g.value === groupBy)?.label ?? "分组";
  const expansionLabel =
    TIME_EXPANSIONS.find((e) => e.value === expansion)?.label ?? "";

  const chartOption = useMemo(
    () =>
      data ? buildCostChartOption({ buckets: data.buckets, groupBy, expansion }) : null,
    [data, groupBy, expansion],
  );

  return (
    <div className="page">
      <header className="page-header">
        <h1>成本归因</h1>
        <div className="page-actions">
          <div className="filter-field">
            <label htmlFor={windowId}>时间窗</label>
            <select
              id={windowId}
              value={hours}
              onChange={(event) => setHours(Number(event.target.value))}
            >
              {TIME_WINDOWS.map((window) => (
                <option key={window.hours} value={window.hours}>
                  {window.label}
                </option>
              ))}
            </select>
          </div>
          <div className="filter-field">
            <label htmlFor={groupId}>分组</label>
            <select
              id={groupId}
              value={groupBy}
              onChange={(event) => setGroupBy(event.target.value)}
            >
              {GROUPINGS.map((grouping) => (
                <option key={grouping.value} value={grouping.value}>
                  {grouping.label}
                </option>
              ))}
            </select>
          </div>
          <div className="filter-field">
            <label htmlFor={expandId}>时间展开</label>
            {/* 主维度是时间时禁用而非隐藏：控件消失会让人以为是自己点错了 */}
            <select
              id={expandId}
              value={expansion}
              disabled={isTimeSeries}
              title={
                isTimeSeries
                  ? "已经按时间分组了，不能再按时间展开"
                  : "在每个分组内再按时间拆开，看谁的花销在涨"
              }
              onChange={(event) => setExpandBy(event.target.value)}
            >
              {TIME_EXPANSIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
        </div>
      </header>

      {error && <ErrorBox error={error} />}
      {isPending && <CardGridSkeleton count={4} />}

      {data && (
        <>
          <div className="stat-cards">
            <StatCard label="总成本" value={formatCost(data.total_cost_usd)} highlight />
            <StatCard label="总 Token" value={formatTokens(data.total_tokens)} />
            <StatCard label="Span 数" value={String(data.span_count)} />
            <StatCard
              label="缓存命中率"
              value={formatPercent(data.cache_hit_ratio)}
              hint="命中率高说明多轮场景实际计费远低于名义 Token"
            />
          </div>

          {/* 总计来自独立的全量聚合，buckets 却有上限。不说清两者不相等，
              用户会拿图去对总额，然后以为其中一个是错的 */}
          {data.truncated && (
            <p className="hint truncate-note">
              分组项过多，下方图表与表格只含成本最高的 {data.buckets.length} 项。
              上方总计是全窗口的完整数值，与图表之和不相等。
              {/* 展开态下桶数是「维度值 × 时间点」，最容易撞上限的就是它，
                  所以先建议关掉展开，而不是让用户去缩时间窗 */}
              {expansion
                ? "时间展开会把桶数乘上时间点数量，改成「不展开」或换用更粗的档位（按天）可以看全。"
                : "换用更粗的分组粒度（按天）或缩小时间窗可以看全。"}
            </p>
          )}

          {data.buckets.length === 0 ? (
            <p className="hint">该时间窗内没有 LLM 调用记录。</p>
          ) : (
            <>
              {chartOption && (
                <Chart
                  option={chartOption}
                  height={expansion ? 380 : 320}
                  title={
                    expansion
                      ? `${groupLabel}的成本趋势（${expansionLabel}）`
                      : `${groupLabel}的成本分布`
                  }
                  ariaLabel={`${groupLabel}的成本${expansion ? "趋势" : "分布"}，共 ${data.buckets.length} 项，总计 ${formatCost(data.total_cost_usd)}。完整数值见下方表格。`}
                  notMerge
                />
              )}

              <table className="data-table">
                <thead>
                  <tr>
                    <th scope="col">{groupLabel}</th>
                    {expansion && <th scope="col">{expansionLabel.replace("按", "")}</th>}
                    <th scope="col" className="num">
                      调用数
                    </th>
                    <th scope="col" className="num">
                      输入
                    </th>
                    <th scope="col" className="num">
                      输出
                    </th>
                    <th scope="col" className="num">
                      缓存读
                    </th>
                    <th scope="col" className="num">
                      缓存写
                    </th>
                    <th scope="col" className="num">
                      推理
                    </th>
                    <th scope="col" className="num">
                      成本
                    </th>
                    {drilldownParam && <th scope="col">下钻</th>}
                  </tr>
                </thead>
                <tbody>
                  {data.buckets.map((bucket, index) => {
                    const value = bucket.key[groupBy];
                    const timeValue = expansion ? bucket.key[expansion] : undefined;
                    return (
                      <tr key={`${value ?? ""}|${timeValue ?? ""}|${index}`}>
                        <td className="mono">{value ?? "—"}</td>
                        {expansion && (
                          <td className="mono">{timeValue?.slice(0, 16) ?? "—"}</td>
                        )}
                        <td className="num">{bucket.span_count}</td>
                        <td className="num">{formatTokens(bucket.input_tokens)}</td>
                        <td className="num">{formatTokens(bucket.output_tokens)}</td>
                        <td className="num">{formatTokens(bucket.cache_read_tokens)}</td>
                        <td className="num">{formatTokens(bucket.cache_write_tokens)}</td>
                        <td className="num">{formatTokens(bucket.reasoning_tokens)}</td>
                        <td className="num cost-value">{formatCost(bucket.cost_usd)}</td>
                        {drilldownParam && (
                          <td>
                            {value ? (
                              <Link
                                className="btn btn-sm"
                                /* 带上 since_hours：不带的话"30 天里花了 $50"点进去
                                   只看到最近 100 条，用户会以为筛选没生效。
                                   展开态下这个链接仍然只按维度值筛全窗口 ——
                                   Span 列表只有 since_hours，没有绝对时间区间，
                                   编不出"就这一小时" */
                                to={`/spans?${drilldownParam}=${encodeURIComponent(value)}&kind=llm&since_hours=${hours}`}
                                title={
                                  expansion
                                    ? `查看 ${value} 在整个时间窗内的 Span（不限于这一${expansionLabel.replace("按", "")}）`
                                    : undefined
                                }
                              >
                                查看 Span
                                <ArrowRight size={12} aria-hidden />
                              </Link>
                            ) : (
                              <span className="hint">—</span>
                            )}
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </>
          )}
        </>
      )}
    </div>
  );
}

function StatCard({
  label,
  value,
  hint,
  highlight = false,
}: {
  label: string;
  value: string;
  hint?: string;
  highlight?: boolean;
}) {
  return (
    <div className={`stat-card ${highlight ? "highlight" : ""}`} title={hint}>
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
    </div>
  );
}
