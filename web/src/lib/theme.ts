/**
 * 主题：日间「新闻纸」/ 夜间「印厂校样」。
 *
 * 机制是 CSS `light-dark()` + `color-scheme`：令牌层每个颜色只写一次
 * （见 tokens.css），切换靠改 <html> 的 color-scheme 生效，因此这里
 * 只需要维护一个属性，不需要给两套主题各维护一份变量表。
 *
 * 三态而非两态：`system` 是缺省，跟随操作系统；用户显式选过之后才
 * 落到 light/dark 并写进 localStorage。少了 system 态就没法"取消选择"，
 * 用户一旦点过就永远脱离系统设置。
 */

import { useCallback, useEffect, useSyncExternalStore } from "react";

export type ThemeChoice = "light" | "dark" | "system";

const STORAGE_KEY = "ariadne.theme";

function isChoice(value: unknown): value is ThemeChoice {
  return value === "light" || value === "dark" || value === "system";
}

function read(): ThemeChoice {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return isChoice(raw) ? raw : "system";
  } catch {
    // 隐私模式下 localStorage 可能抛异常，此时退回跟随系统
    return "system";
  }
}

/** 把选择写到 <html data-theme>。system 时移除属性，交回 CSS 的媒体查询。 */
export function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", choice);
}

const listeners = new Set<() => void>();

function emit(): void {
  for (const fn of listeners) fn();
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  // 跨标签页同步：另一个标签改了主题，这个标签也要跟上
  const onStorage = (e: StorageEvent) => {
    if (e.key === STORAGE_KEY) {
      applyTheme(read());
      emit();
    }
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(fn);
    window.removeEventListener("storage", onStorage);
  };
}

export function setTheme(choice: ThemeChoice): void {
  try {
    localStorage.setItem(STORAGE_KEY, choice);
  } catch {
    // 存不下也要让本次切换生效，只是刷新后回到跟随系统
  }
  applyTheme(choice);
  emit();
}

/**
 * 首帧之前应用主题，避免"先亮后暗"的闪烁。
 * 从 main.tsx 调用，早于 React 挂载。
 */
export function initTheme(): void {
  applyTheme(read());
}

export function useTheme(): {
  choice: ThemeChoice;
  resolved: "light" | "dark";
  setChoice: (c: ThemeChoice) => void;
} {
  const choice = useSyncExternalStore(subscribe, read, () => "system" as const);

  // system 态下要知道实际落到哪一档（图表主题需要真实值，不能传 "system"）
  const resolved = useSyncExternalStore(
    (fn) => {
      const mq = window.matchMedia("(prefers-color-scheme: dark)");
      mq.addEventListener("change", fn);
      const un = subscribe(fn);
      return () => {
        mq.removeEventListener("change", fn);
        un();
      };
    },
    () => {
      const c = read();
      if (c !== "system") return c;
      return window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light";
    },
    () => "light" as const,
  );

  useEffect(() => {
    applyTheme(choice);
  }, [choice]);

  const setChoice = useCallback((c: ThemeChoice) => setTheme(c), []);

  return { choice, resolved, setChoice };
}
