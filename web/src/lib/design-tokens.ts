/**
 * 把 CSS 设计令牌解析成 JS 能用的具体色值。
 *
 * 为什么需要这一层：令牌用 `light-dark(a, b)` 写，而
 * `getComputedStyle(el).getPropertyValue("--x")` 返回的是**未解析的原始
 * 字符串**（实测就是 "light-dark(#f9f9f7, #121210)"），拿去喂给 ECharts /
 * xterm 会直接渲染失败。
 *
 * 解法是借一个探针元素：把令牌赋给 `color` 这个真实属性，浏览器会按当前
 * color-scheme 解析，再读回计算值就是 "rgb(…)"。这样 JS 侧的图表主题与
 * CSS 令牌仍然是**同一个取值来源**，不必维护第二份色表（维护两份的下场
 * 是改了 CSS 忘了改 JS，图表颜色悄悄和徽标对不上）。
 */

let probe: HTMLElement | null = null;

function getProbe(): HTMLElement {
  if (probe?.isConnected) return probe;
  probe = document.createElement("span");
  probe.setAttribute("aria-hidden", "true");
  probe.style.cssText =
    "position:absolute;width:0;height:0;visibility:hidden;pointer-events:none";
  document.body.appendChild(probe);
  return probe;
}

/** 解析单个令牌。fallback 用于 SSR / 测试环境下没有 DOM 的情况。 */
export function readToken(name: string, fallback = "#000000"): string {
  if (typeof document === "undefined") return fallback;
  try {
    const el = getProbe();
    el.style.color = "";
    el.style.color = `var(${name})`;
    const value = getComputedStyle(el).color;
    return value || fallback;
  } catch {
    return fallback;
  }
}

/** 批量解析，省去多次读取触发的样式重算。 */
export function readTokens<K extends string>(
  names: Record<K, string>,
): Record<K, string> {
  const out = {} as Record<K, string>;
  for (const key of Object.keys(names) as K[]) {
    out[key] = readToken(names[key]);
  }
  return out;
}
