/**
 * 未保存修改守卫弹窗。
 *
 * window.confirm 只有「确定/取消」两个选项，而人在这里真正想做的通常是
 * 第三件事：先存下来再走。给三个出口，默认焦点落在「保存并离开」。
 */

import { useEffect, useId, useRef } from "react";

import { useDialogA11y } from "@/lib/useDialogA11y";

interface UnsavedChangesDialogProps {
  /** 具体丢什么，由调用方给（如「3 个节点、名称」），别只说"有修改"。 */
  summary: string;
  saving: boolean;
  onSave: () => void;
  onDiscard: () => void;
  onCancel: () => void;
}

export function UnsavedChangesDialog({
  summary,
  saving,
  onSave,
  onDiscard,
  onCancel,
}: UnsavedChangesDialogProps) {
  const titleId = useId();
  const descId = useId();
  const dialogRef = useDialogA11y(onCancel);
  // 「保存并离开」变 disabled 时焦点会被困住（Tab 陷阱找不到可聚焦的
  // 兜底），退到「留在本页」保键盘可达
  const stayRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (saving) stayRef.current?.focus();
  }, [saving]);

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        // 点遮罩等于「留下」—— 误触不该丢东西
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div
        className="modal modal-narrow"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descId}
        ref={dialogRef}
      >
        <header className="modal-head">
          <h2 id={titleId}>还有未保存的修改</h2>
        </header>
        <div className="modal-body">
          <p className="dialog-text" id={descId}>
            {summary}
          </p>
        </div>
        <footer className="modal-foot modal-foot-split">
          {/* 破坏性动作放左边、样式弱化，和主操作拉开距离，避免顺手点中 */}
          <button type="button" className="btn btn-danger-ghost" onClick={onDiscard}>
            不保存，直接离开
          </button>
          <div className="dialog-actions">
            <button ref={stayRef} type="button" className="btn" onClick={onCancel}>
              留在本页
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={onSave}
              disabled={saving}
            >
              {saving ? "保存中…" : "保存并离开"}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}
