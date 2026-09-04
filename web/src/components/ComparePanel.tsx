/**
 * 实验对比面板 —— 模态壳、取数、焦点管理、结果导出。
 *
 * 本文件只管数据流与编排：查询、键盘/焦点、复制。
 * 指标表见 compare/CompareStatsTable，样本变化见 compare/CompareChurnBlock，
 * 结论与清单见 compare/CompareSummary，判定与标签规则见 lib/compare-format。
 */

import { useQuery } from "@tanstack/react-query";
import { Copy, X } from "lucide-react";
import { useCallback, useId } from "react";

import { api } from "@/api/client";
import { CompareChurnBlock } from "@/components/compare/CompareChurnBlock";
import { CompareStatsTable } from "@/components/compare/CompareStatsTable";
import {
  CompareErrorState,
  GateVerdict,
  IssueList,
  JudgeMismatchNotice,
  ReportText,
} from "@/components/compare/CompareSummary";
import { TableSkeleton } from "@/components/Skeleton";
import { toast } from "@/components/Toast";
import { buildClipboardText } from "@/lib/compare-format";
import { useDialogA11y } from "@/lib/useDialogA11y";

interface Props {
  baselineId: string;
  currentId: string;
  /** 两侧 judge 模型：不一致时分数不可直接比较，必须显著告警 */
  baselineJudgeModels: string[];
  currentJudgeModels: string[];
  onClose: () => void;
}

export function ComparePanel({
  baselineId,
  currentId,
  baselineJudgeModels,
  currentJudgeModels,
  onClose,
}: Props) {
  const titleId = useId();
  // 焦点管理：打开移入、关闭还给触发元素、Escape 关闭、Tab 陷阱
  const dialogRef = useDialogA11y(onClose);

  const { data, isPending, isFetching, error, refetch } = useQuery({
    queryKey: ["compare", baselineId, currentId],
    queryFn: () =>
      api.compareExperiments({ baseline_id: baselineId, current_id: currentId }),
    retry: false,
  });

  const judgeMismatch = baselineJudgeModels.join("|") !== currentJudgeModels.join("|");

  const copyResults = useCallback(() => {
    if (!data) return;
    const text = buildClipboardText(
      data,
      baselineId,
      currentId,
      baselineJudgeModels,
      currentJudgeModels,
    );
    void navigator.clipboard
      .writeText(text)
      .then(() => toast.success("对比结果已复制"))
      .catch(() => toast.error("复制失败，请手动选择文本"));
  }, [data, baselineId, currentId, baselineJudgeModels, currentJudgeModels]);

  const retry = useCallback(() => void refetch(), [refetch]);

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="modal modal-wide compare-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        ref={dialogRef}
      >
        <header className="modal-head">
          <h2 id={titleId}>对比结果</h2>
          <div className="page-actions">
            <button
              type="button"
              className="btn"
              onClick={copyResults}
              disabled={!data}
              title={data ? "把完整对比结果复制为纯文本" : "等对比结果返回后可复制"}
            >
              <Copy size={13} aria-hidden />
              复制结果
            </button>
            <button
              type="button"
              className="close-btn"
              onClick={onClose}
              aria-label="关闭对比"
            >
              <X size={15} aria-hidden />
            </button>
          </div>
        </header>

        <div className="modal-body">
          {/* judge 不一致是破坏性变更：两侧分数不可直接比较，必须先看到这条 */}
          {judgeMismatch && (
            <JudgeMismatchNotice
              baselineJudgeModels={baselineJudgeModels}
              currentJudgeModels={currentJudgeModels}
            />
          )}

          {isPending && (
            <div className="compare-loading" role="status" aria-live="polite">
              <p className="hint">正在对比两次运行的样本级结果…</p>
              <TableSkeleton cols={7} rows={4} />
            </div>
          )}

          {error !== null && (
            <CompareErrorState error={error} onRetry={retry} isRetrying={isFetching} />
          )}

          {data && (
            <>
              <GateVerdict data={data} />
              <CompareStatsTable stats={data.stats} />
              <CompareChurnBlock
                churn={data.churn}
                flippedToFail={data.flipped_to_fail}
              />
              <IssueList title="门禁违规" items={data.violations} variant="violation" />
              <IssueList title="警告" items={data.warnings} variant="warning" />
              <ReportText text={data.report_text} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
