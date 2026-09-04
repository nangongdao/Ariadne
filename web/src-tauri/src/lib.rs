//! Ariadne 桌面端（Tauri 2）。
//!
//! 职责刻意薄：窗口承载 Vite 构建的 React 前端，业务全在前端 + 后端 API。
//! Rust 侧只装配插件——内置终端用 tauri-plugin-shell 拉起 PowerShell，
//! stdin/stdout 经 IPC 流式转发给前端的 xterm.js（见 web/src/lib/terminal.ts）。

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .run(tauri::generate_context!())
        .expect("Ariadne 桌面端启动失败");
}
