/**
 * 状态指示。
 *
 * 无障碍要求：状态不能只靠颜色表达（色盲用户），因此同时用符号 + 文字。
 */

import type { SpanStatus } from "@/api/types";

const STATUS_META: Record<SpanStatus, { symbol: string; label: string; cls: string }> = {
  ok: { symbol: "●", label: "成功", cls: "status-ok" },
  error: { symbol: "✕", label: "失败", cls: "status-error" },
  blocked: { symbol: "⛊", label: "被拦截", cls: "status-blocked" },
};

interface Props {
  status: SpanStatus;
  /** 有告警但未失败（慢/降级/重试），用黄色区分 */
  warning?: boolean;
  showLabel?: boolean;
}

export function StatusDot({ status, warning = false, showLabel = false }: Props) {
  const meta = STATUS_META[status];
  const cls = warning && status === "ok" ? "status-warn" : meta.cls;
  const label = warning && status === "ok" ? "有告警" : meta.label;

  return (
    <span className={`status ${cls}`} role="img" aria-label={label} title={label}>
      <span aria-hidden="true">{meta.symbol}</span>
      {showLabel && <span className="status-text">{label}</span>}
    </span>
  );
}
