/**
 * 对比结论区：judge 一致性告警、门禁结论、违规/警告清单、后端报告全文。
 *
 * 顺序即阅读优先级 —— judge 不一致会让下面所有差值失去意义，必须排在最前。
 */

import { Check, TriangleAlert, X } from "lucide-react";

import type { CompareResponse } from "@/api/types";
import { exitCodeLabel } from "@/lib/compare-format";

/** judge 换版是破坏性变更：整体平移打分，差值主要反映评分器而非被测系统 */
export function JudgeMismatchNotice({
  baselineJudgeModels,
  currentJudgeModels,
}: {
  baselineJudgeModels: string[];
  currentJudgeModels: string[];
}) {
  return (
    <div className="callout callout-warn" role="alert">
      <strong>
        <TriangleAlert size={14} aria-hidden /> Judge 模型不一致，分数不可直接比较
      </strong>
      <p>
        baseline：
        <span className="mono">{baselineJudgeModels.join(", ") || "—"}</span>
        ；current：
        <span className="mono">{currentJudgeModels.join(", ") || "—"}</span>
        。换 judge 会整体平移打分，下面的差值主要反映评分器变化而非被测系统变化。
        要得到可信结论，请用同一 judge 重跑其中一侧。
      </p>
    </div>
  );
}

export function GateVerdict({ data }: { data: CompareResponse }) {
  return (
    <div className={`callout ${data.passed ? "callout-info" : "callout-danger"}`}>
      <strong>
        {data.passed ? <Check size={14} aria-hidden /> : <X size={14} aria-hidden />}{" "}
        {data.passed ? "门禁通过" : "门禁未通过"}
      </strong>
      <p>
        数据集 <span className="mono">{data.dataset_ref}</span>
        {" · "}
        exit_code <span className="mono">{data.exit_code}</span>（
        {exitCodeLabel(data.exit_code)}）
        {data.passed
          ? " —— 未触发任何失败条件，可以合并。"
          : " —— 见下方「门禁违规」逐条排查后再合并。"}
      </p>
    </div>
  );
}

export function IssueList({
  title,
  items,
  variant,
}: {
  title: string;
  items: string[];
  variant: "violation" | "warning";
}) {
  if (items.length === 0) return null;

  return (
    <div className="detail-section">
      <h3>
        {title}
        <span className="badge">{items.length}</span>
      </h3>
      <ul className={variant === "violation" ? "violation-list" : "warning-list"}>
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

/** CI 里贴的就是这段，界面上也得能看到并复制 */
export function ReportText({ text }: { text: string }) {
  if (text.trim() === "") return null;

  return (
    <div className="detail-section">
      <h3>报告全文</h3>
      <pre className="result-output">{text}</pre>
    </div>
  );
}

export function CompareErrorState({
  error,
  onRetry,
  isRetrying,
}: {
  error: unknown;
  onRetry: () => void;
  isRetrying: boolean;
}) {
  return (
    <div className="error-box" role="alert">
      <strong>无法对比</strong>
      <p>{error instanceof Error ? error.message : String(error)}</p>
      <p className="hint">
        两侧数据集不同、实验尚未跑完、或后端未启用对比接口都会导致失败。
        修正后可直接重试，无需重新选择实验。
      </p>
      <button type="button" className="btn btn-sm" onClick={onRetry} disabled={isRetrying}>
        {isRetrying ? "重试中…" : "重试"}
      </button>
    </div>
  );
}
