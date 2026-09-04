/**
 * 内置终端页 —— xterm.js + Tauri shell 桥。
 *
 * 交互细节（管道 shell 的回显约定）：
 * - 用户按键由 xterm 本地回显（打印字符、Backspace 抹字、Enter 换行），
 *   整行提交给 shell 进程；命令历史用 ↑/↓ 翻阅。
 * - 粘贴多行文本时逐行提交。
 * - 浏览器模式（无 Tauri IPC）展示引导卡片，不初始化 xterm。
 */

import "@xterm/xterm/css/xterm.css";

import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import { Eraser, RotateCcw, TerminalSquare } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { isTauri } from "@/api/client";
import { readToken } from "@/lib/design-tokens";
import { TerminalSession, type TerminalShell } from "@/lib/terminal";
import { useTheme } from "@/lib/theme";
import {
  type ArrowKey,
  hasSubmit,
  loadHistory,
  parseTerminalInput,
  pushHistory,
} from "@/lib/terminal-input";

const SHELL_LABEL: Record<TerminalShell, string> = {
  cmd: "CMD",
  powershell: "PowerShell",
};

export function TerminalPage() {
  // 环境判定放在无 hooks 的外壳组件里，避免条件调用 hooks（React 规则）
  if (!isTauri()) {
    return (
      <section className="page">
        <header className="page-header">
          <h1>终端</h1>
          <p className="hint">
            内置终端是桌面端能力（Tauri）。浏览器模式下此页不可用。
          </p>
        </header>
        <div className="terminal-fallback">
          <TerminalSquare aria-hidden size={40} />
          <p>
            桌面端运行方式：
            <code>npm run desktop:dev</code>
            （开发）或
            <code>npm run desktop:build</code>
            （打包）。
          </p>
        </div>
      </section>
    );
  }
  return <TerminalView />;
}

function TerminalView() {
  const hostRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const sessionRef = useRef<TerminalSession | null>(null);
  const lineRef = useRef("");
  const historyRef = useRef<string[]>(loadHistory());
  const historyIdxRef = useRef(-1);
  // handleInput 定义在 startSession 之前，用 ref 转一手才不至于为了
  // 一个回车把整条依赖链倒过来
  const restartRef = useRef<(() => Promise<void>) | null>(null);

  const [shell, setShell] = useState<TerminalShell>("cmd");
  // 终端配色跟随全局主题（见下方就地换色的 effect）
  const { resolved: themeMode } = useTheme();
  // 错误与普通状态（启动中/已结束）要能视觉区分，故带 isError 标记
  const [status, setStatus] = useState<{ text: string; isError: boolean }>({
    text: "",
    isError: false,
  });
  // 会话是否活着。ref 供按键路径读取（handleInput 是 xterm 初始化 effect 的依赖，
  // 挂 state 会让每次状态变化都重建整个终端），state 只用于渲染。
  const runningRef = useRef(false);
  const [running, setRunning] = useState(false);
  const setSessionAlive = useCallback((alive: boolean) => {
    runningRef.current = alive;
    setRunning(alive);
  }, []);

  const commitLine = useCallback(
    (line: string) => {
      const term = termRef.current;
      const session = sessionRef.current;
      if (!term || !session?.running) return;
      const trimmed = line.trim();
      if (trimmed) {
        historyRef.current = pushHistory(historyRef.current, trimmed);
        historyIdxRef.current = -1;
      }
      session.writeLine(line);
    },
    [],
  );

  /** 抹掉当前行的本地回显，换成 next（历史翻阅只整行替换，不做行内光标）。 */
  const replaceLine = useCallback((next: string) => {
    const term = termRef.current;
    if (!term) return;
    term.write("\b \b".repeat(lineRef.current.length));
    term.write(next);
    lineRef.current = next;
  }, []);

  const handleArrow = useCallback(
    (key: ArrowKey) => {
      if (key === "up") {
        if (historyIdxRef.current < historyRef.current.length - 1) {
          historyIdxRef.current += 1;
          replaceLine(historyRef.current[historyIdxRef.current] ?? "");
        }
        return;
      }
      if (key === "down") {
        if (historyIdxRef.current > 0) {
          historyIdxRef.current -= 1;
          replaceLine(historyRef.current[historyIdxRef.current] ?? "");
        } else {
          historyIdxRef.current = -1;
          replaceLine("");
        }
        return;
      }
      // ←/→：管道 shell 只整行提交，没有行内光标可移动，吞掉即可（不回显字面量）
    },
    [replaceLine],
  );

  const handleInput = useCallback(
    (data: string) => {
      const term = termRef.current;
      if (!term) return;

      // 会话已结束时不能继续本地回显：字照样出现在屏幕上，但 commitLine
      // 会静默丢掉，用户敲完回车等于对着一个死终端说话。
      // 这里把 Enter 直接接到重启上 —— 那正是此刻唯一有意义的操作。
      if (!runningRef.current) {
        if (hasSubmit(data)) void restartRef.current?.();
        return;
      }

      for (const action of parseTerminalInput(data)) {
        switch (action.kind) {
          case "text":
            lineRef.current += action.value;
            term.write(action.value);
            break;
          case "backspace":
            if (lineRef.current.length > 0) {
              lineRef.current = lineRef.current.slice(0, -1);
              term.write("\b \b");
            }
            break;
          case "submit":
            term.write("\r\n");
            commitLine(lineRef.current);
            lineRef.current = "";
            historyIdxRef.current = -1;
            break;
          case "arrow":
            handleArrow(action.key);
            break;
        }
      }
    },
    [commitLine, handleArrow],
  );

  const startSession = useCallback(
    async (which: TerminalShell) => {
      const term = termRef.current;
      if (!term) return;
      setStatus({ text: "正在启动…", isError: false });
      setSessionAlive(false);
      lineRef.current = "";
      await sessionRef.current?.kill();
      const session = new TerminalSession();
      sessionRef.current = session;
      await session.start(which, {
        onData: (chunk) => term.write(chunk),
        onReady: () => {
          setStatus({ text: "", isError: false });
          setSessionAlive(true);
          term.write(`\x1b[38;2;45;212;191mAriadne 终端已就绪（${SHELL_LABEL[which]}）\x1b[0m\r\n`);
        },
        onClose: (code) => {
          setStatus({
            text: `会话已结束${code === null ? "" : `（exit ${code}）`}`,
            isError: code !== null && code !== 0,
          });
          setSessionAlive(false);
          term.write(
            `\r\n\x1b[38;2;148;163;184m[会话结束 · 按 Enter 或点「重启」开新会话]\x1b[0m\r\n`,
          );
        },
        onError: (message) => {
          setStatus({ text: `错误: ${message}`, isError: true });
          setSessionAlive(false);
          term.write(`\r\n\x1b[38;2;248;113;113m${message}\x1b[0m\r\n`);
        },
      });
    },
    [setSessionAlive],
  );

  restartRef.current = () => startSession(shell);

  // xterm 初始化（仅一次）
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const term = new Terminal({
      fontFamily: "'JetBrains Mono', Consolas, 'Cascadia Mono', monospace",
      fontSize: 13,
      lineHeight: 1.4,
      cursorBlink: true,
      convertEol: true,
      scrollback: 5000,
      // 从 CSS 令牌取色，随日/夜主题走。写死色值会让终端在日间主题下
      // 仍是一块深色矩形，与整页的"纸"格格不入
      theme: {
        background: readToken("--bg-elev", "#ffffff"),
        foreground: readToken("--text", "#111111"),
        cursor: readToken("--accent", "#cc0000"),
        selectionBackground: readToken("--accent-soft", "#eeeeee"),
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    fit.fit();
    term.onData(handleInput);
    termRef.current = term;
    fitRef.current = fit;

    const observer = new ResizeObserver(() => fitRef.current?.fit());
    observer.observe(host);

    return () => {
      observer.disconnect();
      void sessionRef.current?.kill();
      term.dispose();
      termRef.current = null;
    };
  }, [handleInput]);

  // 主题切换时就地换色，不重建终端 —— 重建会丢掉回滚缓冲和正在跑的会话
  useEffect(() => {
    const term = termRef.current;
    if (!term) return;
    term.options.theme = {
      background: readToken("--bg-elev", "#ffffff"),
      foreground: readToken("--text", "#111111"),
      cursor: readToken("--accent", "#cc0000"),
      selectionBackground: readToken("--accent-soft", "#eeeeee"),
    };
  }, [themeMode]);

  // shell 切换时重启会话
  useEffect(() => {
    void startSession(shell);
  }, [shell, startSession]);

  return (
    <section className="page page-full">
      <header className="page-header">
        <h1>终端</h1>
        <div className="terminal-actions">
          <div className="seg-control" role="tablist" aria-label="shell 选择">
            {(Object.keys(SHELL_LABEL) as TerminalShell[]).map((s) => (
              <button
                key={s}
                type="button"
                role="tab"
                aria-selected={shell === s}
                className={shell === s ? "active" : ""}
                onClick={() => {
                  // 切换 shell 直接 kill 正在跑的进程：跑着的命令/状态会
                  // 瞬间丢光，得先让用户确认过再换，而不是静默杀掉
                  if (s !== shell && runningRef.current) {
                    const ok = window.confirm(
                      `切换到 ${SHELL_LABEL[s]} 会终止当前正在运行的会话，确定切换？`,
                    );
                    if (!ok) return;
                  }
                  setShell(s);
                }}
              >
                {SHELL_LABEL[s]}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="btn-ghost"
            onClick={() => termRef.current?.clear()}
            title="清屏"
          >
            <Eraser size={14} aria-hidden />
            清屏
          </button>
          <button
            type="button"
            className={running ? "btn-ghost" : "btn-primary"}
            onClick={() => void startSession(shell)}
            title="重启会话"
          >
            <RotateCcw size={14} aria-hidden />
            重启
          </button>
        </div>
      </header>
      <div className="terminal-panel">
        {/* role=application：内部自管键盘（含方向键），让读屏器交出按键而不是走浏览阅读模式 */}
        <div
          ref={hostRef}
          className="terminal-host"
          role="application"
          aria-label={
            running
              ? `内置终端（${SHELL_LABEL[shell]}），输入命令后按 Enter 执行，上下方向键翻阅历史`
              : `内置终端（${SHELL_LABEL[shell]}）会话已结束，按 Enter 开新会话`
          }
          tabIndex={0}
          onFocus={(event) => {
            // 焦点落在宿主上时转交给 xterm 的输入区；内部 textarea 冒泡上来的不重复处理
            if (event.target === event.currentTarget) termRef.current?.focus();
          }}
        />
      </div>
      {status.text ? (
        <p
          className={`terminal-status ${status.isError ? "form-error" : "hint"}`}
          {...(status.isError ? { role: "alert" as const } : {})}
        >
          {status.text}
          {!running && (
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => void startSession(shell)}
            >
              <RotateCcw size={12} aria-hidden />
              重启会话
            </button>
          )}
        </p>
      ) : null}
    </section>
  );
}
