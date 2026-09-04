/**
 * Loop 的可执行动作 —— 批准/拒绝/取消/恢复。
 *
 * 列表行和详情页要的是同一套东西：同样的四个 mutation、同样的两处失效、
 * 同样的确认弹窗。抽出来是因为之前只有列表页有，详情页一个 HUMAN_PENDING
 * 的 Loop 每 5 秒轮询一次却没有任何能点的东西。
 *
 * 不可逆的动作（拒绝、取消）走 window.confirm —— 与图编辑器等处一致。
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";

import { api, ApiError } from "@/api/client";
import type { LoopSummary } from "@/api/types";

/** 终态：不能再转移，approve/cancel/resume 一律 409（loop_runs.py transition）。 */
const TERMINAL_STATES: ReadonlySet<string> = new Set([
  "CONVERGED",
  "REJECTED",
  "BLOCKED",
  "BUDGET_EXCEEDED",
  "MAX_ITERATIONS",
  "STALLED",
  "FAILED",
  "CANCELLED",
]);

export interface LoopActions {
  /** 待人工审批：批准与拒绝按钮该出现 */
  canApprove: boolean;
  /** 非终态：可取消 */
  canCancel: boolean;
  /**
   * 非终态且非待审批：可重新入队。
   * 待审批时不给 —— 那时该走审批，resume 会把状态直接推走绕过审批。
   */
  canResume: boolean;
  /** 任一动作在途。四个动作共享，避免连点 */
  isPending: boolean;
  /** 最近一次失败的原因，成功后清空 */
  error: string | null;
  approve: () => void;
  reject: () => void;
  cancel: () => void;
  resume: () => void;
}

export function useLoopActions(loop: Pick<LoopSummary, "id" | "state">): LoopActions {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const loopId = loop.id;

  const refresh = useCallback(() => {
    setError(null);
    void queryClient.invalidateQueries({ queryKey: ["loops"] });
    void queryClient.invalidateQueries({ queryKey: ["loop", loopId] });
    void queryClient.invalidateQueries({ queryKey: ["loop-iterations", loopId] });
  }, [queryClient, loopId]);

  const fail = useCallback((what: string) => {
    return (e: unknown) => {
      // 409 的 message 已经说清"已是终态"，直接用；其余给个兜底
      setError(e instanceof ApiError ? e.message : `${what}失败`);
    };
  }, []);

  const approveMutation = useMutation({
    mutationFn: (approved: boolean) => api.approveLoop(loopId, approved),
    onSuccess: refresh,
    onError: fail("审批"),
  });

  const cancelMutation = useMutation({
    mutationFn: () => api.cancelLoop(loopId),
    onSuccess: refresh,
    onError: fail("取消"),
  });

  const resumeMutation = useMutation({
    mutationFn: () => api.resumeLoop(loopId),
    onSuccess: refresh,
    onError: fail("恢复"),
  });

  const isTerminal = TERMINAL_STATES.has(loop.state);
  const isAwaitingHuman = loop.state === "HUMAN_PENDING";

  return {
    canApprove: isAwaitingHuman,
    canCancel: !isTerminal,
    canResume: !isTerminal && !isAwaitingHuman,
    isPending:
      approveMutation.isPending || cancelMutation.isPending || resumeMutation.isPending,
    error,
    approve: () => approveMutation.mutate(true),
    reject: () => {
      // 拒绝直接转 REJECTED 终态，不可撤销
      if (!window.confirm("拒绝后 Loop 立即终止且无法恢复。确定拒绝？")) return;
      approveMutation.mutate(false);
    },
    cancel: () => {
      if (!window.confirm("取消后 Loop 转入终态，无法再继续。确定取消？")) return;
      cancelMutation.mutate();
    },
    resume: () => resumeMutation.mutate(),
  };
}
