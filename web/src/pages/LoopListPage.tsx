import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { api } from "@/api/client";
import type { LoopSummary } from "@/api/types";
import { LOOP_MODE_LABELS, LOOP_STATE_LABELS, TERMINAL_DIAGNOSIS } from "@/api/types";
import { CreateLoopForm } from "@/components/CreateLoopForm";
import { TableSkeleton } from "@/components/Skeleton";
import {
  formatAbsoluteTime,
  formatCost,
  formatRelativeTime,
  shortId,
} from "@/lib/format";
import { ACTIVE_LOOP_STATES, MODE_OPTIONS } from "@/lib/loop-options";
import { useLoopActions } from "@/lib/useLoopActions";
import { ErrorBox } from "@/pages/TraceListPage";

/** 列表轮询间隔。全部 Loop 到终态后停止（见 refetchInterval）。 */
const POLL_MS = 8000;

function isActive(loop: LoopSummary): boolean {
  return ACTIVE_LOOP_STATES.has(loop.state);
}

function LoopsFilter({
  state,
  mode,
  onStateChange,
  onModeChange,
}: {
  state: string;
  mode: string;
  onStateChange: (v: string) => void;
  onModeChange: (v: string) => void;
}) {
  return (
    <>
      <select
        className="filter-select"
        value={state}
        onChange={(e) => onStateChange(e.target.value)}
        aria-label="按状态过滤"
      >
        <option value="">全部状态</option>
        <option value="active">运行中</option>
        {Object.entries(LOOP_STATE_LABELS).map(([s, label]) => (
          <option key={s} value={s}>
            {label}
          </option>
        ))}
      </select>
      <select
        className="filter-select"
        value={mode}
        onChange={(e) => onModeChange(e.target.value)}
        aria-label="按模式过滤"
      >
        <option value="">全部模式</option>
        {MODE_OPTIONS.map((m) => (
          <option key={m.value} value={m.value}>
            {m.label}
          </option>
        ))}
      </select>
    </>
  );
}


export function LoopListPage() {
  const navigate = useNavigate();
  // 筛选条件进 URL：刷新/分享链接不丢条件
  const [searchParams, setSearchParams] = useSearchParams();
  const stateFilter = searchParams.get("state") ?? "";
  const modeFilter = searchParams.get("mode") ?? "";

  function setFilterParam(key: "state" | "mode", value: string) {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    setSearchParams(next, { replace: true });
  }

  // 筛选下推服务端：后端 state 只认单个 LoopState 枚举值，
  // "active" 是前端聚合的 9 个状态，无法下推 —— 该档仍需本地筛。
  // 空串即「不带该参数」：request() 会跳过空值（见 client.ts）。
  const serverState = stateFilter === "active" ? "" : stateFilter;

  const { data, isPending, error } = useQuery({
    queryKey: ["loops", serverState, modeFilter],
    queryFn: () => api.listLoops({ limit: 100, state: serverState, mode: modeFilter }),
    // 有 Loop 在跑才轮询；全部到终态后数据不再变，继续轮询纯属浪费。
    // 列表为空时保持轮询 —— 新 Loop 可能由别处（CLI/SDK）创建。
    refetchInterval: (query) => {
      const rows = query.state.data;
      if (!rows || rows.length === 0) return POLL_MS;
      return rows.some(isActive) ? POLL_MS : false;
    },
  });
  const loops = data ?? [];

  // SSE：运行中的 Loop 实时刷新（event 到达即 invalidate）。
  // 依赖必须是「活跃 id 列表」派生的稳定字符串，不能是 loops 数组本身：
  // 回调里的 invalidateQueries 会让 react-query 产出新数组引用 → effect 重跑
  // → 所有 EventSource 断开重连 → 又收事件 → 再重连，形成自激重连风暴。
  const activeIdsKey = useMemo(
    () =>
      loops
        .filter(isActive)
        .map((l) => l.id)
        .sort()
        .join(","),
    [loops],
  );

  const queryClient = useQueryClient();
  useEffect(() => {
    if (!activeIdsKey) return;
    const controllers = activeIdsKey.split(",").map((id) =>
      api.streamLoopEvents(id, () => {
        void queryClient.invalidateQueries({ queryKey: ["loops"] });
      }),
    );
    // 卸载或活跃集合变化时断开全部连接
    return () => controllers.forEach((c) => c.abort());
  }, [activeIdsKey, queryClient]);

  // 服务端已按 state/mode 过滤；只有 "active" 这档需要本地收敛
  const filtered = useMemo(
    () => (stateFilter === "active" ? loops.filter(isActive) : loops),
    [loops, stateFilter],
  );

  return (
    <div className="page">
      <header className="page-header">
        <h1>Loop</h1>
        <span className="hint">不达标自动迭代到达标 · 实时 SSE 刷新</span>
      </header>

      {error && <ErrorBox error={error} />}

      <div className="loop-layout">
        <div className="loop-main">
          <div className="page-header" style={{ minHeight: 0 }}>
            <LoopsFilter
              state={stateFilter}
              mode={modeFilter}
              onStateChange={(v) => setFilterParam("state", v)}
              onModeChange={(v) => setFilterParam("mode", v)}
            />
            <span className="hint">{filtered.length} 个</span>
          </div>

          {isPending ? (
            <TableSkeleton cols={7} rows={8} />
          ) : filtered.length === 0 ? (
            <div className="empty-state">
              {stateFilter || modeFilter ? (
                <>
                  <p>当前筛选条件下没有 Loop。</p>
                  <p className="hint">
                    换个状态/模式，或
                    <button
                      type="button"
                      className="btn btn-ghost"
                      onClick={() => setSearchParams(new URLSearchParams(), { replace: true })}
                    >
                      清除筛选
                    </button>
                  </p>
                </>
              ) : (
                <>
                  <p>还没有 Loop 记录。</p>
                  <p className="hint">
                    用左侧表单或 <code>POST /v1/loops</code> 创建。
                  </p>
                </>
              )}
            </div>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">任务</th>
                  <th scope="col">状态</th>
                  <th scope="col" className="num">
                    轮次
                  </th>
                  <th scope="col" className="num">
                    Token
                  </th>
                  <th scope="col" className="num">
                    成本
                  </th>
                  <th scope="col">创建</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((loop) => (
                  <LoopRow key={loop.id} loop={loop} />
                ))}
              </tbody>
            </table>
          )}
        </div>

        <aside className="loop-sidebar" aria-labelledby="create-loop-title">
          <h2 id="create-loop-title">创建 Loop</h2>
          <p className="hint">给定目标与达标条件，Loop 会自动迭代到达标或用尽预算。</p>
          {/* 创建成功直接进详情页，省去用户在表格里翻找新 Loop */}
          <CreateLoopForm onCreated={(loopId) => navigate(`/loops/${loopId}`)} />
        </aside>
      </div>
    </div>
  );
}

function LoopRow({ loop }: { loop: LoopSummary }) {
  // 取消/拒绝是不可逆的终态转移，hook 里带 confirm 守卫
  const actions = useLoopActions(loop);
  const terminal = loop.final_state
    ? TERMINAL_DIAGNOSIS[loop.final_state] ?? null
    : null;

  return (
    <tr>
      <td>
        <Link to={`/loops/${loop.id}`} className="link-strong">
          {loop.goal.task}
        </Link>
        <span className="hint block mono" style={{ fontSize: 12 }}>
          {LOOP_MODE_LABELS[loop.mode] ?? loop.mode} · {shortId(loop.id, 8)}
        </span>
      </td>
      <td>
        <span className={`tag status-tag-${loop.state.toLowerCase()}`}>
          {LOOP_STATE_LABELS[loop.state] ?? loop.state}
        </span>
        {terminal && (
          <span className="hint block" title={terminal}>
            {terminal}
          </span>
        )}
      </td>
      <td className="num">{loop.iteration}</td>
      <td className="num">{loop.cumulative_tokens.toLocaleString()}</td>
      <td className="num cost-value">{formatCost(loop.cumulative_cost_usd)}</td>
      <td
        title={loop.created_at ? formatAbsoluteTime(loop.created_at) : undefined}
      >
        {loop.created_at ? formatRelativeTime(loop.created_at) : "—"}
      </td>
      <td>
        <div className="btn-row">
          {actions.canApprove && (
            <>
              <button
                type="button"
                className="btn btn-sm btn-primary"
                disabled={actions.isPending}
                onClick={actions.approve}
              >
                批准
              </button>
              <button
                type="button"
                className="btn btn-sm btn-danger"
                disabled={actions.isPending}
                onClick={actions.reject}
              >
                拒绝
              </button>
            </>
          )}
          {actions.canCancel && (
            <button
              type="button"
              className="btn btn-sm"
              disabled={actions.isPending}
              onClick={actions.cancel}
              title="转 CANCELLED"
            >
              取消
            </button>
          )}
        </div>
        {actions.error && (
          <span className="hint block" role="alert">
            {actions.error}
          </span>
        )}
      </td>
    </tr>
  );
}