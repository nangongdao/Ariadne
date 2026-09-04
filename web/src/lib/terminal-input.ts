/**
 * 终端按键解析 —— 把 xterm 送来的原始字节流拆成一串动作。
 *
 * 单独成文件是为了让转义序列这段能脱离 React 单测：方向键的形态有两种，
 * 粘贴时又会和普通字符混在一个 chunk 里，逻辑比看上去绕。
 */

export type ArrowKey = "up" | "down" | "right" | "left";

/**
 * 方向键发来的是完整转义序列：CSI 形态 `\x1b[A`，应用光标键模式下是 SS3
 * 形态 `\x1bOA`。逐码点遍历会把它拆成 `\x1b` + `[` + `A`，历史分支永远不命中，
 * 字面量 `[A` 反而被插进行缓冲——所以必须先整段匹配完整序列。
 */
const ARROW_SEQUENCES: Record<string, ArrowKey> = {
  "\x1b[A": "up",
  "\x1b[B": "down",
  "\x1b[C": "right",
  "\x1b[D": "left",
  "\x1bOA": "up",
  "\x1bOB": "down",
  "\x1bOC": "right",
  "\x1bOD": "left",
};

export type TerminalAction =
  | { kind: "text"; value: string }
  | { kind: "backspace" }
  | { kind: "submit" }
  | { kind: "arrow"; key: ArrowKey };

/**
 * 从 `chars[start]`（值为 ESC）起，返回该转义序列最后一个字符的下标（含）。
 * 认两种形态：CSI = `\x1b[` + 参数字节 + 终止符（0x40–0x7e）；SS3 = `\x1bO` + 单字符。
 * 认不出来时返回 start，调用方只吞掉 ESC 本身。
 */
function escapeSequenceEnd(chars: readonly string[], start: number): number {
  const second = chars[start + 1];
  if (second === "[") {
    for (let i = start + 2; i < chars.length; i += 1) {
      const ch = chars[i];
      if (ch === undefined) break;
      const code = ch.codePointAt(0) ?? 0;
      if (code >= 0x40 && code <= 0x7e) return i;
    }
    // 序列被切在 chunk 边界：整段吞掉，别让残片落进缓冲
    return chars.length - 1;
  }
  if (second === "O") {
    return chars[start + 2] === undefined ? start + 1 : start + 2;
  }
  return start;
}

/**
 * 解析一个输入 chunk。
 *
 * 连续的可打印字符会合成一个 text 动作，粘贴一大段时只需一次 term.write，
 * 而不是每个字符一次。
 */
export function parseTerminalInput(data: string): TerminalAction[] {
  const actions: TerminalAction[] = [];
  const chars = Array.from(data);
  let buffer = "";

  const flush = (): void => {
    if (buffer) {
      actions.push({ kind: "text", value: buffer });
      buffer = "";
    }
  };

  for (let i = 0; i < chars.length; i += 1) {
    const ch = chars[i];
    if (ch === undefined) continue;

    if (ch === "\x1b") {
      flush();
      const end = escapeSequenceEnd(chars, i);
      const arrow = ARROW_SEQUENCES[chars.slice(i, end + 1).join("")];
      if (arrow) actions.push({ kind: "arrow", key: arrow });
      // 其余序列（Home/End/Delete/F 键…）整段丢弃，不落进行缓冲
      i = end;
      continue;
    }

    if (ch === "\r" || ch === "\n") {
      if (ch === "\r" && chars[i + 1] === "\n") i += 1; // CRLF 算一次提交
      flush();
      actions.push({ kind: "submit" });
    } else if (ch === "\x7f" || ch === "\b") {
      flush();
      actions.push({ kind: "backspace" });
    } else if (ch >= " ") {
      buffer += ch;
    }
    // 其余控制字符忽略
  }

  flush();
  return actions;
}

/** chunk 里是否含提交键 —— 会话已结束时只需知道"用户按了回车"。 */
export function hasSubmit(data: string): boolean {
  return data.includes("\r") || data.includes("\n");
}

// ---------- 命令历史 ----------

const HISTORY_KEY = "ariadne.terminal.history";
const HISTORY_LIMIT = 100;

export function loadHistory(): string[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? (parsed as string[]) : [];
  } catch {
    return [];
  }
}

/** 去重后置顶并持久化，返回新历史（原数组不变）。 */
export function pushHistory(history: readonly string[], entry: string): string[] {
  const next = [entry, ...history.filter((h) => h !== entry)].slice(0, HISTORY_LIMIT);
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(next));
  } catch {
    // 存储满/禁用只影响历史持久化，不影响功能
  }
  return next;
}
