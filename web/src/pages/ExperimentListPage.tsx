import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeftRight, RefreshCw, Target, TriangleAlert } from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import { api } from "@/api/client";
import type { ExperimentSummary } from "@/api/types";
import { ComparePanel } from "@/components/ComparePanel";
import { TableSkeleton } from "@/components/Skeleton";
import {
  formatAbsoluteTime,
  formatCost,
  formatRelativeTime,
  shortId,
} from "@/lib/format";
import { ErrorBox } from "@/pages/TraceListPage";

const STATUS_LABELS: Record<string, string> = {
  pending: "待运行",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

/** 未进入终态的实验还会变，只有这时才需要轮询 */
const LIVE_STATUSES = new Set(["pending", "running"]);

/**
 * 后端 list_experiments 的 limit 上限（Query(ge=1, le=200)），且没有 offset ——
 * 不存在真正的分页，前端无论如何拿不到更多。取满上限，触顶时明确告知被截断，
 * 而不是让第 201 条静默消失。
 */
const MAX_LIMIT = 200;

/** 数据集引用形如 `name#version`，列表里只展示 name 部分 */
function datasetName(ref: string): string {
  return ref.split("#")[0] ?? ref;
}

function metricOf(experiment: ExperimentSummary, key: string): number | null {
  const value = experiment.metrics[key];
  return typeof value === "number" ? value : null;
}

/** 非系统指标的其他指标（如 faithfulness）。后端两个系统键之外全在这里，
    不列出的话 eval 评了个存在不出声，用户以为没跑 */
function extraMetrics(experiment: ExperimentSummary): Array<[string, number]> {
  const system = new Set(["composite_quality", "assertion_pass_rate"]);
  return Object.entries(experiment.metrics)
    .filter(([k, v]) => !system.has(k) && typeof v === "number")
    .map(([k, v]) => [k, v as number]);
}

export function ExperimentListPage() {
  // 基线/当前分开存：原来用数组按勾选顺序隐式决定谁是基线，
  // 界面上看不出来也换不了，换成两个显式的槽位
  const [baselineId, setBaselineId] = useState<string | null>(null);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [comparing, setComparing] = useState(false);

  const [datasetFilter, setDatasetFilter] = useState("");

  // 无过滤的全量（最多 200）：既是表格在未筛选时的数据源，也给「数据集下拉」
  // 去重、给基线/当前兜底。与过滤查询分开 —— 否则选数据集时已选的基线会消失
  const { data: allData, isFetching: allFetching, error: allError } = useQuery({
    queryKey: ["experiments-all"],
    queryFn: () => api.listExperiments({ limit: MAX_LIMIT }),
    // 全部进终态后停止轮询：原来固定 8s 会一直空转
    refetchInterval: (query) =>
      (query.state.data ?? []).some((e) => LIVE_STATUSES.has(e.status)) ? 8000 : false,
  });

  // 表格数据：选了数据集就走服务端过滤（能看到该集 200 条之后的实验，
  // 客户端筛永远只看得到最近 200 条）。未筛选时不重复请求
  const {
    data: filteredData,
    isPending: filteredPending,
    isFetching: filteredFetching,
    error: filteredError,
  } = useQuery({
    queryKey: ["experiments", datasetFilter],
    queryFn: () => api.listExperiments({ limit: MAX_LIMIT, dataset_ref: datasetFilter }),
    enabled: datasetFilter !== "",
  });

  const isPending = datasetFilter === "" ? Boolean(!allData) : filteredPending;
  const isFetching = datasetFilter === "" ? allFetching : filteredFetching;
  const error = datasetFilter === "" ? allError : filteredError;
  const queryClient = useQueryClient();
  // 刷新当前视图：未筛选刷全量，选中数据集刷过滤查询
  const refreshView = () => {
    const key =
      datasetFilter === ""
        ? ["experiments-all"]
        : (["experiments", datasetFilter] as [string, string]);
    void queryClient.invalidateQueries({ queryKey: key });
  };

  const allExperiments = allData ?? [];
  const experiments = datasetFilter === "" ? allExperiments : (filteredData ?? []);
  const truncated = experiments.length >= MAX_LIMIT;

  // 数据集选项取完整 ref（name#version）去重；服务端过滤要的是完整 ref，
  // 不能拿 /v1/datasets 的 name 来顶。当前选中的值并进选项，避免选中一个
  // 未出现在最近 200 条里的数据集后选项消失、再也选不回别的
  const datasetRefs = useMemo(() => {
    const refs = new Set(allExperiments.map((e) => e.dataset_ref));
    if (datasetFilter !== "") refs.add(datasetFilter);
    return [...refs].sort();
  }, [allExperiments, datasetFilter]);

  // 从全量里找：筛选只影响表格显示，不该让已选中的基线/当前"消失"
  const baseline = allExperiments.find((e) => e.id === baselineId) ?? null;
  const current = allExperiments.find((e) => e.id === currentId) ?? null;

  // 只允许同数据集的实验参与对比：在不同数据集上比均值毫无意义，
  // 后端也会拒绝，这里提前禁用避免用户白点一次
  const comparableRef = useMemo(
    () => baseline?.dataset_ref ?? current?.dataset_ref ?? null,
    [baseline, current],
  );

  const pickBaseline = useCallback((experiment: ExperimentSummary) => {
    setBaselineId((prev) => (prev === experiment.id ? null : experiment.id));
    setCurrentId((prev) => (prev === experiment.id ? null : prev));
  }, []);

  const pickCurrent = useCallback((experiment: ExperimentSummary) => {
    setCurrentId((prev) => (prev === experiment.id ? null : experiment.id));
    setBaselineId((prev) => (prev === experiment.id ? null : prev));
  }, []);

  const swap = useCallback(() => {
    setBaselineId(currentId);
    setCurrentId(baselineId);
  }, [baselineId, currentId]);

  const clearPicks = useCallback(() => {
    setBaselineId(null);
    setCurrentId(null);
  }, []);

  function pickable(experiment: ExperimentSummary): boolean {
    if (experiment.status !== "completed") return false;
    if (experiment.id === baselineId || experiment.id === currentId) return true;
    if (comparableRef === null) return true;
    return experiment.dataset_ref === comparableRef;
  }

  function disabledReason(experiment: ExperimentSummary): string | undefined {
    if (experiment.status !== "completed") return "只有已完成的实验可对比";
    if (pickable(experiment)) return undefined;
    return "只能与同数据集的实验对比";
  }

  const bothPicked = baseline !== null && current !== null;
  const judgeMismatch =
    bothPicked && baseline.judge_models.join("|") !== current.judge_models.join("|");

  return (
    <div className="page">
      <header className="page-header">
        <h1>实验</h1>
        <div className="page-actions">
          <span className="hint">选定基线与当前两个同数据集的已完成实验</span>
          {/* 对比要求同数据集，按数据集收窄是主路径而非附加功能 */}
          {datasetRefs.length > 1 && (
            <select
              className="filter-select"
              value={datasetFilter}
              onChange={(e) => setDatasetFilter(e.target.value)}
              aria-label="按数据集过滤"
            >
              <option value="">全部数据集（{datasetRefs.length}）</option>
              {datasetRefs.map((ref) => (
                <option key={ref} value={ref}>
                  {datasetName(ref)}
                </option>
              ))}
            </select>
          )}
          <button
            type="button"
            className="btn"
            onClick={refreshView}
            disabled={isFetching}
          >
            <RefreshCw size={13} aria-hidden />
            {isFetching ? "刷新中…" : "刷新"}
          </button>
        </div>
      </header>

      {error && <ErrorBox error={error} />}

      {truncated && (
        <p className="hint">
          仅显示最近 {MAX_LIMIT} 个实验（接口上限，无分页）。更早的记录请按数据集收窄，
          或用 <code>GET /v1/experiments?dataset_ref=…</code> 直接查询。
        </p>
      )}

      {/* 显式展示谁是基线、谁是当前，并允许互换 */}
      {(baseline !== null || current !== null) && (
        <div className="compare-pick">
          <span>
            基线：
            {baseline === null ? (
              <span className="hint">未选</span>
            ) : (
              <span className="link-strong">{baseline.config_label}</span>
            )}
          </span>
          <ArrowLeftRight size={13} aria-hidden />
          <span>
            当前：
            {current === null ? (
              <span className="hint">未选</span>
            ) : (
              <span className="link-strong">{current.config_label}</span>
            )}
          </span>
          <button
            type="button"
            className="btn btn-sm"
            onClick={swap}
            disabled={!bothPicked}
            aria-label="互换基线与当前"
          >
            <ArrowLeftRight size={13} aria-hidden />
            互换
          </button>
          <button
            type="button"
            className="btn btn-sm btn-primary"
            onClick={() => setComparing(true)}
            disabled={!bothPicked}
          >
            开始对比
          </button>
          <button type="button" className="btn btn-sm" onClick={clearPicks}>
            清空
          </button>
          {judgeMismatch && (
            <span className="badge-warn">
              <TriangleAlert size={12} aria-hidden />
              Judge 不一致
            </span>
          )}
        </div>
      )}

      {comparing && bothPicked && (
        <ComparePanel
          baselineId={baseline.id}
          currentId={current.id}
          baselineJudgeModels={baseline.judge_models}
          currentJudgeModels={current.judge_models}
          onClose={() => setComparing(false)}
        />
      )}

      {isPending ? (
        <TableSkeleton cols={8} rows={8} />
      ) : experiments.length === 0 ? (
        <div className="empty-state">
          {datasetFilter === "" ? (
            <>
              <p>还没有实验记录。</p>
              <p className="hint">
                用 SDK 跑实验后回传结果，或用
                <code> ariadne-eval </code>
                在本地对比。
              </p>
            </>
          ) : (
            <>
              <p>「{datasetName(datasetFilter)}」下没有实验。</p>
              <p className="hint">
                换个数据集，或
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => setDatasetFilter("")}
                >
                  查看全部
                </button>
              </p>
            </>
          )}
        </div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">对比</th>
              <th scope="col">配置</th>
              <th scope="col">数据集</th>
              <th scope="col">状态</th>
              <th scope="col" className="num">
                样本
              </th>
              <th scope="col" className="num">
                质量分
              </th>
              <th scope="col" className="num">
                通过率
              </th>
              <th scope="col" className="num">
                成本
              </th>
              <th scope="col">Judge</th>
              <th scope="col">创建</th>
            </tr>
          </thead>
          <tbody>
            {experiments.map((experiment) => {
              const quality = metricOf(experiment, "composite_quality");
              const passRate = metricOf(experiment, "assertion_pass_rate");
              const extras = extraMetrics(experiment);
              const enabled = pickable(experiment);
              const reason = disabledReason(experiment);
              const isBaseline = experiment.id === baselineId;
              const isCurrent = experiment.id === currentId;

              return (
                <tr key={experiment.id} className={isBaseline || isCurrent ? "selected" : ""}>
                  <td>
                    <div className="page-actions">
                      <button
                        type="button"
                        className={isBaseline ? "btn btn-sm btn-primary" : "btn btn-sm"}
                        aria-pressed={isBaseline}
                        disabled={!enabled}
                        onClick={() => pickBaseline(experiment)}
                        aria-label={`把 ${experiment.config_label} 设为基线`}
                        {...(reason === undefined ? {} : { title: reason })}
                      >
                        <Target size={12} aria-hidden />
                        基线
                      </button>
                      <button
                        type="button"
                        className={isCurrent ? "btn btn-sm btn-primary" : "btn btn-sm"}
                        aria-pressed={isCurrent}
                        disabled={!enabled}
                        onClick={() => pickCurrent(experiment)}
                        aria-label={`把 ${experiment.config_label} 设为当前`}
                        {...(reason === undefined ? {} : { title: reason })}
                      >
                        <Target size={12} aria-hidden />
                        当前
                      </button>
                    </div>
                  </td>
                  <td className="link-strong">{experiment.config_label}</td>
                  <td className="mono" title={experiment.dataset_ref}>
                    {datasetName(experiment.dataset_ref)}
                  </td>
                  <td>
                    <span className={`tag status-tag-${experiment.status}`}>
                      {STATUS_LABELS[experiment.status] ?? experiment.status}
                    </span>
                    {experiment.failed_count > 0 && (
                      <span
                        className="error-count"
                        title={`${experiment.failed_count} 个样本生成失败（不计入均值）`}
                      >
                        {experiment.failed_count} 失败
                      </span>
                    )}
                    {/* 后端一直在返回 error，之前没人渲染 —— 状态标签只说"失败"，
                        不说为什么失败 */}
                    {experiment.error && (
                      <span className="hint block" title={experiment.error}>
                        {experiment.error}
                      </span>
                    )}
                  </td>
                  <td className="num">{experiment.item_count}</td>
                  <td className="num">
                    {quality === null ? "—" : quality.toFixed(2)}
                  </td>
                  <td className="num">
                    {passRate === null ? "—" : `${(passRate * 100).toFixed(1)}%`}
                    {extras.length > 0 && (
                      // 非系统指标（faithfulness 等）后端会返回但不显示，
                      // 数字就丢了。这里作为小字第二行列出，不开新列
                      <span className="extra-metrics">
                        {extras.map(([k, v]) => (
                          <span key={k} title={k}>
                            {k} {typeof v === "number" ? v.toFixed(2) : String(v)}
                          </span>
                        ))}
                      </span>
                    )}
                  </td>
                  <td className="num cost-value">
                    {formatCost(experiment.total_cost_usd)}
                  </td>
                  <td>
                    {experiment.judge_models.length === 0 ? (
                      <span className="hint">—</span>
                    ) : (
                      [...new Set(experiment.judge_models)].map((model) => (
                        <span key={model} className="tag tag-sm mono">
                          {model}
                        </span>
                      ))
                    )}
                  </td>
                  <td title={formatAbsoluteTime(experiment.created_at)}>
                    {formatRelativeTime(experiment.created_at)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {baseline !== null && current === null && (
        <p className="hint">
          已设基线（{shortId(baseline.id, 8)}）。再把一个同数据集的实验设为「当前」即可对比。
        </p>
      )}
      {current !== null && baseline === null && (
        <p className="hint">
          已设当前（{shortId(current.id, 8)}）。再把一个同数据集的实验设为「基线」即可对比。
        </p>
      )}
    </div>
  );
}
