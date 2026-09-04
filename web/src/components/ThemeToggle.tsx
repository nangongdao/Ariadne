/**
 * 主题切换 —— 日间 / 夜间 / 跟随系统。
 *
 * 宽屏用三态分段控件：图标切换在三态下无法表达"跟随系统"，用户也看不出
 * 当前到底是哪一档；三个按钮各自 aria-pressed，读屏器能直接读出选中项。
 *
 * 窄屏放不下三个按钮（顶栏还要留给位置、搜索、环境状态），改成单个循环
 * 按钮：图标显示当前档位，点一下往下一档走。这样触控目标能给足 44×44，
 * 而不是把三个按钮各挤到 28px —— 后者两头都不讨好。
 *
 * 两套控件同时在 DOM 里，由 CSS 断点决定显示哪个（display:none 会一并
 * 移出无障碍树，不会出现读屏器读到两份的问题）。不用 JS 测视口，
 * 免得旋转屏幕或拖窗口时状态不同步。
 */

import { Monitor, Moon, Sun } from "lucide-react";

import { useTheme, type ThemeChoice } from "@/lib/theme";

type Option = { value: ThemeChoice; label: string; Icon: typeof Sun };

const OPTIONS: readonly Option[] = [
  { value: "light", label: "日间", Icon: Sun },
  { value: "dark", label: "夜间", Icon: Moon },
  { value: "system", label: "跟随系统", Icon: Monitor },
];

/** 用 Record 而不是数组下标环绕：下标在 noUncheckedIndexedAccess 下是可选类型，
 *  而档位就三个、循环顺序是固定的，写成映射表更直白也免了非空断言。 */
const META: Record<ThemeChoice, Option & { next: ThemeChoice }> = {
  light: { value: "light", label: "日间", Icon: Sun, next: "dark" },
  dark: { value: "dark", label: "夜间", Icon: Moon, next: "system" },
  system: { value: "system", label: "跟随系统", Icon: Monitor, next: "light" },
};

export function ThemeToggle() {
  const { choice, setChoice } = useTheme();
  const current = META[choice];
  const next = META[current.next];
  const CurrentIcon = current.Icon;

  return (
    <>
      <div className="theme-toggle" role="group" aria-label="配色主题">
        {OPTIONS.map(({ value, label, Icon }) => (
          <button
            key={value}
            type="button"
            className={`theme-toggle-btn${choice === value ? " active" : ""}`}
            aria-pressed={choice === value}
            title={label}
            onClick={() => setChoice(value)}
          >
            <Icon size={13} aria-hidden />
            <span className="sr-only">{label}</span>
          </button>
        ))}
      </div>

      {/* 窄屏：单键循环。可访问名带上当前与下一档，点之前就知道会切到哪 */}
      <button
        type="button"
        className="theme-cycle"
        title={`配色主题：${current.label}（点击切到${next.label}）`}
        aria-label={`配色主题，当前${current.label}，点击切到${next.label}`}
        onClick={() => setChoice(next.value)}
      >
        <CurrentIcon size={16} aria-hidden />
      </button>
    </>
  );
}
