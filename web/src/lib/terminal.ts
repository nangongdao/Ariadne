/**
 * 内置终端会话 —— 封装 Tauri shell 插件，为 xterm.js 提供 stdin/stdout 桥。
 *
 * 设计约束：
 * - 桌面端专属。浏览器模式没有 IPC，TerminalPage 层负责展示降级提示。
 * - v1 用管道（非 PTY）：cmd.exe /q 是逐行刷新的管道良民；PowerShell
 *   原生命令偶有缓冲。回显由 xterm 本地处理（管道 shell 不回显输入），
 *   所以 cmd 用 /q 关掉它自己的回显，避免双重显示。
 * - stdout/stderr 按行推送（插件按换行切分），写入 xterm 时补回 \r\n。
 * - 升级路径：ConPTY（portable-pty）可获得完整 TTY（颜色/光标控制），
 *   届时替换 start/write 的实现，TerminalPage 不用动。
 */

import { Command, type Child } from "@tauri-apps/plugin-shell";

export type TerminalShell = "cmd" | "powershell";

export interface TerminalCallbacks {
  /** shell 输出（stdout/stderr 合流，按行） */
  onData: (chunk: string) => void;
  onReady: (shell: TerminalShell) => void;
  onClose: (code: number | null) => void;
  onError: (message: string) => void;
}

/** 各 shell 的启动参数（见 capabilities/default.json 的 spawn 白名单） */
const SHELL_ARGS: Record<TerminalShell, string[]> = {
  cmd: ["/q"],
  powershell: ["-NoLogo"],
};

export class TerminalSession {
  private child: Child | null = null;
  private shell: TerminalShell = "cmd";

  get running(): boolean {
    return this.child !== null;
  }

  get currentShell(): TerminalShell {
    return this.shell;
  }

  async start(shell: TerminalShell, cb: TerminalCallbacks): Promise<void> {
    await this.kill();
    this.shell = shell;
    const command = Command.create(shell, SHELL_ARGS[shell]);

    command.stdout.on("data", (line: string) => {
      cb.onData(line + "\r\n");
    });
    command.stderr.on("data", (line: string) => {
      cb.onData(line + "\r\n");
    });
    command.on("close", ({ code }: { code: number | null }) => {
      this.child = null;
      cb.onClose(code);
    });
    command.on("error", (err: unknown) => {
      cb.onError(err instanceof Error ? err.message : String(err));
    });

    this.child = await command.spawn();
    cb.onReady(shell);
  }

  /** 提交一行命令（管道模式无回显，回显由 xterm 侧本地处理）。 */
  writeLine(line: string): void {
    void this.child?.write(line + "\n");
  }

  async kill(): Promise<void> {
    if (this.child) {
      const child = this.child;
      this.child = null;
      try {
        await child.kill();
      } catch {
        // 进程已退出时 kill 会报错——会话结束本来就是预期路径
      }
    }
  }
}
