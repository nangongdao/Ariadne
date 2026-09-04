/**
 * 模型配置页的三个通用控件：provider 选择器、复制按钮、删除二次确认。
 *
 * 三者都在表单与卡片间复用，故集中在此，与 graph/GraphPanels 同一思路。
 */

import { Check, Copy, Trash2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { toast } from "@/components/Toast";
import type { ModelProvider } from "@/api/types";
import { copyText, PROVIDERS } from "@/lib/model-config";

/**
 * Provider 单选组。
 *
 * 原来这里挂的是 role="tab"，但它并不切换任何 tabpanel —— 只是在表单里
 * 选一个值，读屏会承诺一个不存在的面板。改用 radiogroup + roving tabindex：
 * 组内只有一个可 Tab 到的按钮，左右/上下键在选项间移动并即时选中。
 */
export function ProviderPicker({
  value,
  labelId,
  onChange,
}: {
  value: ModelProvider;
  labelId: string;
  onChange: (provider: ModelProvider) => void;
}) {
  const groupRef = useRef<HTMLDivElement>(null);

  const move = (delta: number) => {
    const index = PROVIDERS.findIndex((p) => p.value === value);
    const next = PROVIDERS[(index + delta + PROVIDERS.length) % PROVIDERS.length];
    if (!next) return;
    onChange(next.value);
    // 选中项就是唯一 tabindex=0 的按钮，焦点得跟着走
    const buttons = groupRef.current?.querySelectorAll("button");
    buttons?.[PROVIDERS.indexOf(next)]?.focus();
  };

  return (
    <div
      ref={groupRef}
      role="radiogroup"
      aria-labelledby={labelId}
      className="seg-control seg-provider"
      onKeyDown={(e) => {
        if (e.key === "ArrowRight" || e.key === "ArrowDown") {
          e.preventDefault();
          move(1);
        } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
          e.preventDefault();
          move(-1);
        }
      }}
    >
      {PROVIDERS.map((p) => {
        const selected = p.value === value;
        return (
          <button
            key={p.value}
            type="button"
            role="radio"
            aria-checked={selected}
            tabIndex={selected ? 0 : -1}
            className={selected ? "active" : ""}
            onClick={() => onChange(p.value)}
            title={p.hint}
          >
            {/* 选中态不只靠颜色，补一个勾 */}
            {selected && <Check size={12} aria-hidden />}
            {p.label}
          </button>
        );
      })}
    </div>
  );
}

const COPIED_MS = 1600;

/**
 * 点击复制。
 *
 * 原来是挂在 <dd> 上的 onClick，键盘完全到不了；且 clipboard 缺失时
 * 静默无反应。现在是真按钮 + 失败必提示。
 */
export function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), COPIED_MS);
    return () => window.clearTimeout(timer);
  }, [copied]);

  return (
    <button
      type="button"
      className="btn-icon copy-btn"
      aria-label={copied ? `${label}已复制` : `复制${label}`}
      title={`复制${label}`}
      onClick={() => {
        void copyText(text).then((ok) => {
          if (ok) {
            setCopied(true);
            toast.success(`${label}已复制`);
          } else {
            toast.error(`复制失败，请手动选中复制（${label}）`);
          }
        });
      }}
    >
      {copied ? (
        <Check size={12} aria-hidden className="copy-ok" />
      ) : (
        <Copy size={12} aria-hidden />
      )}
    </button>
  );
}

/**
 * 删除二次确认 —— 就地展开，点名要删的是哪个配置。
 *
 * 焦点默认落在「取消」：删除不可撤销，误触回车不该等于确认。
 */
export function DeleteConfirm({
  name,
  pending,
  onConfirm,
  onCancel,
}: {
  name: string;
  pending: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    cancelRef.current?.focus();
  }, []);

  return (
    <div
      role="alertdialog"
      aria-label={`确认删除 ${name}`}
      className="delete-confirm"
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          onCancel();
        }
      }}
    >
      <p className="delete-confirm-text">
        删除「{name}」？此操作不可撤销，用它的 Loop 将回退到默认配置。
      </p>
      <div className="delete-confirm-actions">
        <button
          ref={cancelRef}
          type="button"
          className="btn btn-sm"
          onClick={onCancel}
          disabled={pending}
        >
          <X size={12} aria-hidden />
          取消
        </button>
        <button
          type="button"
          className="btn btn-sm btn-danger"
          onClick={onConfirm}
          disabled={pending}
        >
          <Trash2 size={12} aria-hidden />
          {pending ? "删除中…" : "确认删除"}
        </button>
      </div>
    </div>
  );
}
