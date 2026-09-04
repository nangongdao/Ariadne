/**
 * 轻量 Toast 通知 —— 命令式调用，App 根部挂一个 <ToastHost/>。
 *
 * 用法：toast.success("已保存") / toast.error("删除失败：…")
 *
 * 设计约束：只动 transform/opacity；自动 2.6s 消失，离场先播
 * 0.2s 退场动画再移除；最多同时保留 4 条，防刷屏。
 */

import { CheckCircle2, XCircle } from "lucide-react";
import { useEffect, useState } from "react";

interface ToastItem {
  id: number;
  kind: "ok" | "err";
  text: string;
  leaving: boolean;
}

type Listener = (item: ToastItem) => void;

const listeners = new Set<Listener>();
let seq = 0;

function emit(kind: ToastItem["kind"], text: string): void {
  const item: ToastItem = { id: ++seq, kind, text, leaving: false };
  for (const listener of listeners) listener(item);
}

export const toast = {
  success: (text: string) => emit("ok", text),
  error: (text: string) => emit("err", text),
};

const SHOW_MS = 2600;
const LEAVE_MS = 200;
const MAX_VISIBLE = 4;

export function ToastHost() {
  const [items, setItems] = useState<ToastItem[]>([]);

  useEffect(() => {
    const timers = new Set<number>();
    const leave = (id: number) => {
      setItems((prev) =>
        prev.map((t) => (t.id === id ? { ...t, leaving: true } : t)),
      );
      timers.add(
        window.setTimeout(() => {
          setItems((prev) => prev.filter((t) => t.id !== id));
        }, LEAVE_MS),
      );
    };
    const listener: Listener = (item) => {
      setItems((prev) => [...prev.slice(-(MAX_VISIBLE - 1)), item]);
      timers.add(window.setTimeout(() => leave(item.id), SHOW_MS));
    };
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
      for (const t of timers) window.clearTimeout(t);
    };
  }, []);

  if (!items.length) return null;

  return (
    <div className="toast-host" role="status" aria-live="polite">
      {items.map((t) => (
        <div key={t.id} className={`toast ${t.kind}${t.leaving ? " leaving" : ""}`}>
          {t.kind === "ok" ? (
            <CheckCircle2 size={14} aria-hidden />
          ) : (
            <XCircle size={14} aria-hidden />
          )}
          <span>{t.text}</span>
        </div>
      ))}
    </div>
  );
}
