/**
 * 导航守卫 —— 路由是 <BrowserRouter>（无 data router），useBlocker 不可用，
 * 所以在所有客户端跳转入口（侧栏 NavLink、Ctrl+K 面板、顶栏状态按钮）处
 * 显式询问已注册的守卫：允许即放行，否则由守卫接管（弹未保存对话框）。
 *
 * 守卫由「有未保存状态」的页面注册（目前只有图编辑器），离开页面时注销。
 */

type LeaveGuard = (to: string) => boolean;

let guard: LeaveGuard | null = null;

export const navigationGuard = {
  register(fn: LeaveGuard): () => void {
    guard = fn;
    return () => {
      if (guard === fn) guard = null;
    };
  },
  /** 返回 true 表示放行；false 表示已被守卫拦截，调用方不应继续跳转 */
  enter(to: string): boolean {
    return guard === null || guard(to);
  },
};