import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Check,
  ChevronRight,
  Lightbulb,
  Play,
  Repeat,
  RotateCcw,
  ThumbsDown,
  X,
} from "lucide-react";
import { useEffect, useMemo } from "react";
import { Link, useParams } from "react-router-dom";

import { api } from "@/api/client";
import type { LoopAssertion, LoopIteration, LoopSummary } from "@/api/types";
import { LOOP_MODE_LABELS, LOOP_STATE_LABELS, TERMINAL_DIAGNOSIS } from "@/api/types";
import { Chart } from "@/components/ChartLazy";
import { TableSkeleton } from "@/components/Skeleton";
import { formatCost, formatRelativeTime, shortId } from "@/lib/format";
import { useLoopActions } from "@/lib/useLoopActions";
import { ErrorBox } from "@/pages/TraceListPage";

const EXECUTING_STATES = new Set([
  "CREATED",
  "VALIDATE",
  "PLANNING",
  "PRECHECK",
  "EXECUTING",
  "EVALUATING",
  "JUDGING",
  "REVISING",
  "HUMAN_PENDING",
]);

export function LoopDetailPage() {
  const { loopId = "" } = useParams();
  const queryClient = useQueryClient();

  const { data: loop, isPending, error } = useQuery({
    queryKey: ["loop", loopId],
    queryFn: () => api.getLoop(loopId),
    // 运行中才轮询；终态数据不会再变，继续轮询纯属浪费
    refetchInterval: (query) =>
      query.state.data && EXECUTING_STATES.has(query.state.data.state) ? 5000 : false,
  });

  const isRunning = loop ? EXECUTING_STATES.has(loop.state) : false;

  const { data: iterationsData, isPending: iterPending } = useQuery({
    queryKey: ["loop-iterations", loopId],
    queryFn: () => api.loopIterations(loopId),
    refetchInterval: isRunning ? 5000 : false,
  });
  const iterations = iterationsData?.iterations ?? [];

  // SSE 推送即失效重取，比轮询更快看到新轮次。
  // 依赖只能是 loopId + isRunning：依赖整个 loop 对象会让每次轮询返回的新引用
  // 都重建一次 SSE 连接。
  useEffect(() => {
    if (!loopId || !isRunning) return;
    const controller = api.streamLoopEvents(loopId, () => {
      void queryClient.invalidateQueries({ queryKey: ["loop", loopId] });
      void queryClient.invalidateQueries({ queryKey: ["loop-iterations", loopId] });
    });
    return () => controller.abort();
  }, [loopId, isRunning, queryClient]);

  if (isPending)
    return (
      <div className="page">
        <TableSkeleton cols={4} rows={7} />
      </div>
    );
  if (error || !loop)
    return (
      <div className="page">
        <ErrorBox error={error ?? new Error("Loop 不存在")} />
        <Link to="/loops">← 返回 Loop 列表</Link>
      </div>
    );

  const terminal = loop.final_state ? TERMINAL_DIAGNOSIS[loop.final_state] : null;

  return (
    <div className="page">
      <nav className="breadcrumb" aria-label="面包屑">
        <Link to="/loops">Loop</Link>
        <ChevronRight size={13} aria-hidden />
        <span aria-current="page">{shortId(loop.id, 8)}</span>
      </nav>

      <header className="page-head">
        <div className="page-head-main">
          <h1 className="page-title">{loop.goal.task}</h1>
          <div className="page-sub">
            <span className={`state-tag state-${loop.state.toLowerCase()}`}>
              {LOOP_STATE_LABELS[loop.state] ?? loop.state}
            </span>
            <span className="sub-sep" aria-hidden />
            <span>{LOOP_MODE_LABELS[loop.mode] ?? loop.mode}</span>
            <span className="sub-sep" aria-hidden />
            <span className="mono">{shortId(loop.id, 12)}</span>
            {isRunning && (
              <span className="live-pill" title="正在接收实时事件">
                <span className="live-dot" aria-hidden />
                实时
              </span>
            )}
          </div>
        </div>
        <LoopActionBar loop={loop} />
      </header>

      <div className="stat-row">
        <Stat label="轮次" value={String(loop.iteration)} />
        <Stat label="累计 Token" value={loop.cumulative_tokens.toLocaleString()} />
        <Stat label="累计成本" value={formatCost(loop.cumulative_cost_usd)} />
        <Stat
          label="创建于"
          value={loop.created_at ? formatRelativeTime(loop.created_at) : "—"}
        />
      </div>

      {loop.state === "HUMAN_PENDING" && <AwaitingApprovalCallout />}

      {loop.error && (
        <div className="callout callout-danger" role="alert">
          <AlertTriangle size={15} aria-hidden />
          <div>
            <strong>执行失败</strong>
            <p>{loop.error}</p>
          </div>
        </div>
      )}
      {terminal && (
        <div className="callout callout-info">
          <Lightbulb size={15} aria-hidden />
          <p>{terminal}</p>
        </div>
      )}

      {iterPending ? (
        <TableSkeleton cols={5} rows={4} />
      ) : iterations.length > 0 ? (
        <>
          <Charts iterations={iterations} />
          <IterationTable
            iterations={iterations}
            assertionsBy={new Map(loop.goal.assertions.map((a) => [a.id, a]))}
          />
        </>
      ) : (
        <div className="empty-state">
          <span className="empty-icon" aria-hidden>
            <Repeat size={22} />
          </span>
          <h2>还没有轮次记录</h2>
          <p>
            {isRunning
              ? "Loop 正在执行，第一轮结果出来后会自动显示每轮得分与修正指令。"
              : "这个 Loop 尚未产生轮次。若长期停在此状态，检查 Worker 是否在运行。"}
          </p>
        </div>
      )}
    </div>
  );
}

/**
 * 头部动作栏。
 *
 * 详情页原来一个按钮都没有：HUMAN_PENDING 的 Loop 在这儿每 5 秒轮询一次，
 * 但要批准得回列表页找到那一行。现在就地能审批、取消、重新入队。
 */
function LoopActionBar({ loop }: { loop: LoopSummary }) {
  const actions = useLoopActions(loop);

  if (!actions.canApprove && !actions.canCancel && !actions.canResume) return null;

  return (
    <div className="page-head-actions">
      <div className="btn-row">
        {actions.canApprove && (
          <>
            <button
              type="button"
              className="btn btn-primary"
              disabled={actions.isPending}
              onClick={actions.approve}
            >
              <Check size={14} aria-hidden />
              批准继续
            </button>
            <button
              type="button"
              className="btn btn-danger"
              disabled={actions.isPending}
              onClick={actions.reject}
            >
              <ThumbsDown size={14} aria-hidden />
              拒绝
            </button>
          </>
        )}
        {actions.canResume && (
          <button
            type="button"
            className="btn"
            disabled={actions.isPending}
            onClick={actions.resume}
            title="转回校验并重新入队。Worker 崩了或长时间不推进时用"
          >
            <RotateCcw size={14} aria-hidden />
            重新入队
          </button>
        )}
        {actions.canCancel && (
          <button
            type="button"
            className="btn btn-ghost"
            disabled={actions.isPending}
            onClick={actions.cancel}
          >
            <X size={14} aria-hidden />
            取消
          </button>
        )}
      </div>
      {actions.error && (
        <p className="form-error" role="alert">
          {actions.error}
        </p>
      )}
    </div>
  );
}

/** HUMAN_PENDING 说明。状态标签只说"待审批"，不说在等谁、批了会怎样。 */
function AwaitingApprovalCallout() {
  return (
    <div className="callout callout-warn">
      <Play size={15} aria-hidden />
      <div>
        <strong>等待你审批</strong>
        <p>
          Loop 已停在此处，不会自行继续。批准后从当前检查点续跑，拒绝则立即终止。
        </p>
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat-card">
      <span className="stat-label">{label}</span>
      <strong className="stat-value">{value}</strong>
    </div>
  );
}

function Charts({ iterations }: { iterations: LoopIteration[] }) {
  // option 必须 memo：ECharts 每次拿到新对象引用就重跑一次 setOption 全量 diff，
  // 而这个页面轮询 + SSE 会频繁重渲染。
  const { scoreOption, tokenOption, latestScore, convergedAt } = useMemo(() => {
    // compute_score 返回的就是 0-100（verifier/base.py），不要再乘一次：
    // 乘完 80 分会变成 8000，而 yAxis 封顶 100，整条线被裁到图外看不见
    const scores = iterations.map((it) => it.verdict?.score ?? 0);
    const tokens = iterations.map((it) => it.cumulative_tokens);
    const labels = iterations.map((it) => `#${it.iteration}`);
    // 收敛只看 blocking 断言是否全过，与得分无关，所以标注真实收敛的那一轮，
    // 而不是画一条并不存在的"达标线"
    const convergedIndex = iterations.findIndex((it) => it.verdict?.converged);

    return {
      latestScore: scores.at(-1) ?? 0,
      convergedAt: convergedIndex >= 0 ? iterations[convergedIndex]?.iteration : undefined,
      scoreOption: {
        tooltip: { trigger: "axis", valueFormatter: (v: number) => `${v} 分` },
        grid: { left: 44, right: 20, top: 16, bottom: 28 },
        xAxis: { type: "category", data: labels, boundaryGap: false },
        yAxis: { type: "value", min: 0, max: 100 },
        series: [
          {
            type: "line",
            name: "得分",
            data: scores,
            areaStyle: { opacity: 0.14 },
            ...(convergedIndex >= 0
              ? {
                  markLine: {
                    symbol: "none",
                    silent: true,
                    data: [
                      {
                        xAxis: convergedIndex,
                        label: { formatter: "收敛", position: "insideEndTop" },
                      },
                    ],
                    lineStyle: { type: "dashed", color: "#3fb950" },
                  },
                }
              : {}),
          },
        ],
      },
      tokenOption: {
        tooltip: { trigger: "axis" },
        grid: { left: 62, right: 20, top: 16, bottom: 28 },
        xAxis: { type: "category", data: labels },
        yAxis: { type: "value" },
        series: [
          { type: "bar", name: "累计 Token", data: tokens, barMaxWidth: 28, color: "#4c8dff" },
        ],
      },
    };
  }, [iterations]);

  return (
    <div className="loop-charts">
      <Chart
        title="得分趋势"
        option={scoreOption}
        height={224}
        ariaLabel={`得分趋势折线图，共 ${iterations.length} 轮，最新得分 ${latestScore.toFixed(1)} 分${
          convergedAt === undefined ? "，尚未收敛" : `，第 ${convergedAt} 轮收敛`
        }`}
      />
      <Chart
        title="累计 Token"
        option={tokenOption}
        height={224}
        ariaLabel={`累计 Token 柱状图，共 ${iterations.length} 轮，详细数值见下方轮次表`}
      />
    </div>
  );
}

function IterationTable({
  iterations,
  assertionsBy,
}: {
  iterations: LoopIteration[];
  /** id → 断言的人类可读称呼。id 对不上时回退到裸 id，不猜 */
  assertionsBy: Map<string, LoopAssertion>;
}) {
  return (
    <table className="data-table" style={{ marginTop: 16 }}>
      <thead>
        <tr>
          <th scope="col">轮次</th>
          <th scope="col" className="num">
            得分
          </th>
          <th scope="col">失败断言</th>
          <th scope="col">修正指令（critique）</th>
          <th scope="col" className="num">
            累计 Token
          </th>
        </tr>
      </thead>
      <tbody>
        {iterations.map((it) => {
          const verdict = it.verdict;
          const failed = verdict?.failed ?? [];
          const allPassed = failed.length === 0 && verdict?.converged === true;
          const directives = it.critique?.directives;
          return (
            <tr key={it.iteration}>
              <td>#{it.iteration}</td>
              <td className="num">
                {verdict === null ? (
                  "—"
                ) : (
                  <ScoreBadge score={verdict.score} converged={verdict.converged} />
                )}
                {verdict?.false_completion && (
                  <span className="tag tag-danger tag-sm" title="模型自称完成但断言未过">
                    假完成
                  </span>
                )}
              </td>
              <td>
                {allPassed ? (
                  <span className="inline-ok">
                    <Check size={13} aria-hidden />
                    全部通过
                  </span>
                ) : failed.length > 0 ? (
                  <FailedAssertionList failed={failed} assertionsBy={assertionsBy} />
                ) : (
                  <span className="hint">—</span>
                )}
              </td>
              <td className="cell-wrap">
                {directives && directives.length > 0 ? (
                  directives.join("；")
                ) : (
                  <span className="hint">—</span>
                )}
              </td>
              <td className="num">{it.cumulative_tokens.toLocaleString()}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** 失败断言读起来要像人话：断言名 + 证据，而不是一截裸 id */
function FailedAssertionList({
  failed,
  assertionsBy,
}: {
  failed: NonNullable<LoopIteration["verdict"]>["failed"];
  assertionsBy: Map<string, LoopAssertion>;
}) {
  return (
    <ul className="failed-assertions">
      {failed.map((f) => {
        const def = assertionsBy.get(f.assertion_id);
        return (
          <li key={f.assertion_id}>
            <span className="mono failed-id">
              {def?.spec && "pattern" in def.spec ? `/${def.spec.pattern}/` : f.assertion_id}
            </span>
            {def && def.hint && <span className="failed-hint">{def.hint}</span>}
            {f.evidence && (
              <span className="failed-evidence" title={f.evidence}>
                {f.evidence}
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function ScoreBadge({ score, converged }: { score: number; converged: boolean }) {
  return (
    <span
      className={
        converged ? "score-badge score-badge-pass" : "score-badge score-badge-fail"
      }
    >
      {score.toFixed(1)}
    </span>
  );
}