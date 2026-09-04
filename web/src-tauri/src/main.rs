// 防止 release 构建弹出控制台窗口；dev 构建保留日志
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    ariadne_desktop_lib::run()
}
