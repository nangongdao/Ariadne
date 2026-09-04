/**
 * 模态框无障碍行为 —— 焦点移入/归还、Escape 关闭、Tab 焦点陷阱。
 *
 * 抽成 hook 是因为焦点陷阱手写两遍必坏一遍。每次按键都重新查询可聚焦元素，
 * 这样异步到达的内容（如对比结果里的按钮）也能进入循环。
 */

import { useEffect, useRef, type RefObject } from "react";

const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex="-1"])';

/**
 * 挂载即接管焦点，卸载时还给触发元素。
 *
 * @param onClose Escape 时调用。允许传内联箭头函数。
 * @returns 挂到模态框根节点的 ref。
 */
export function useDialogA11y(onClose: () => void): RefObject<HTMLDivElement | null> {
  const dialogRef = useRef<HTMLDivElement>(null);
  // onClose 常以内联箭头函数传入，每次渲染都是新引用；放进 ref 才能让下面的
  // 挂载副作用只跑一次，否则会反复抢焦点
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const timer = window.setTimeout(() => {
      dialogRef.current?.querySelector<HTMLElement>(FOCUSABLE)?.focus();
    }, 0);
    return () => {
      window.clearTimeout(timer);
      opener?.focus?.();
    };
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCloseRef.current();
        return;
      }
      if (e.key !== "Tab") return;
      const root = dialogRef.current;
      if (!root) return;
      const items = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null,
      );
      const first = items[0];
      const last = items[items.length - 1];
      if (first === undefined || last === undefined) return;
      const active = document.activeElement;
      if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      } else if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (active !== null && !root.contains(active)) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return dialogRef;
}
