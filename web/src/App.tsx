import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { Suspense, lazy, useEffect, useState, type ComponentType } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";

import { CommandPalette } from "@/components/CommandPalette";
import { ToastHost } from "@/components/Toast";
import { TopBar } from "@/components/TopBar";
import { navigationGuard } from "@/lib/navigation-guard";
import { NAV_GROUPS } from "@/nav";

// 路由级代码分割：每个页面一个独立 chunk，首屏只加载 Trace 列表所需的
// 代码。React Flow（dagre+xyflow）、ECharts 等重组件因此不进入首屏。
// 页面均为命名导出且不接收 props，这里统一做 named → default 适配。
const lazyNamed = (
  load: () => Promise<Record<string, unknown>>,
  key: string,
): ComponentType =>
  lazy(async () => {
    const mod = await load();
    return { default: mod[key] as ComponentType };
  });

const TraceListPage = lazyNamed(
  () => import("@/pages/TraceListPage"),
  "TraceListPage",
);
const TraceDetailPage = lazyNamed(
  () => import("@/pages/TraceDetailPage"),
  "TraceDetailPage",
);
const SpanListPage = lazyNamed(() => import("@/pages/SpanListPage"), "SpanListPage");
const LoopListPage = lazyNamed(() => import("@/pages/LoopListPage"), "LoopListPage");
const LoopDetailPage = lazyNamed(
  () => import("@/pages/LoopDetailPage"),
  "LoopDetailPage",
);
const ExperimentListPage = lazyNamed(
  () => import("@/pages/ExperimentListPage"),
  "ExperimentListPage",
);
const DatasetListPage = lazyNamed(
  () => import("@/pages/DatasetListPage"),
  "DatasetListPage",
);
const GraphListPage = lazyNamed(() => import("@/pages/GraphListPage"), "GraphListPage");
const GraphEditorPage = lazyNamed(
  () => import("@/pages/GraphEditorPage"),
  "GraphEditorPage",
);
const PlaygroundPage = lazyNamed(
  () => import("@/pages/PlaygroundPage"),
  "PlaygroundPage",
);
const ModelConfigPage = lazyNamed(
  () => import("@/pages/ModelConfigPage"),
  "ModelConfigPage",
);
const CostPage = lazyNamed(() => import("@/pages/CostPage"), "CostPage");
const SettingsPage = lazyNamed(() => import("@/pages/SettingsPage"), "SettingsPage");
const TerminalPage = lazyNamed(() => import("@/pages/TerminalPage"), "TerminalPage");

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 2000,
      // 401 重试没有意义，且会掩盖"key 配错了"这个真实原因
      retry: (failureCount, error) => {
        const status =
          typeof error === "object" && error !== null && "status" in error
            ? Number((error as { status: unknown }).status)
            : 0;
        if (status >= 400 && status < 500) return false;
        return failureCount < 2;
      },
      refetchOnWindowFocus: false,
    },
  },
});

const PageFallback = () => (
  <div className="page">
    <div className="skeleton skeleton-page" aria-hidden />
  </div>
);

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Shell />
      <ToastHost />
    </QueryClientProvider>
  );
}

function Shell() {
  const [cmdkOpen, setCmdkOpen] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const { pathname } = useLocation();

  // 全局 Ctrl/Cmd+K 呼出命令面板；Escape 关抽屉
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setCmdkOpen((open) => !open);
        return;
      }
      if (e.key === "Escape") setNavOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // 导航后自动收起抽屉：移动端点完链接留着遮罩是最常见的挫败点
  useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

  return (
    <>
      <div className="app">
        <nav className={navOpen ? "sidebar open" : "sidebar"} aria-label="主导航">
          <div className="brand">
            <span className="brand-mark" aria-hidden>
              <svg width="22" height="22" viewBox="0 0 1024 1024" fill="none">
                <rect width="1024" height="1024" rx="200" fill="#0B1220" />
                <path
                  d="M200 830 C 390 280, 630 920, 824 194"
                  stroke="url(#brandGrad)"
                  strokeWidth="42"
                  strokeLinecap="round"
                />
                <circle cx="200" cy="830" r="38" fill="#2DD4BF" />
                <circle cx="824" cy="194" r="38" fill="#22D3EE" />
                <defs>
                  <linearGradient
                    id="brandGrad"
                    x1="200"
                    y1="830"
                    x2="824"
                    y2="194"
                    gradientUnits="userSpaceOnUse"
                  >
                    <stop stopColor="#2DD4BF" />
                    <stop offset="1" stopColor="#22D3EE" />
                  </linearGradient>
                </defs>
              </svg>
            </span>
            <span className="brand-text">
              <span className="brand-name">Ariadne</span>
              <span className="brand-sub">观测 · 评测 · Loop</span>
            </span>
          </div>
          <div className="nav-groups">
            {NAV_GROUPS.map((group) => (
              <div className="nav-group" key={group.title}>
                <p className="nav-group-title">{group.title}</p>
                <ul>
                  {group.items.map((item) => (
                    <li key={item.to}>
                      <NavLink
                        to={item.to}
                        className={({ isActive }) =>
                          isActive ? "nav-link active" : "nav-link"
                        }
                        onClick={(e) => {
                          // 点到当前页面是 no-op，不必拦；确实要离开才走守卫
                          if (pathname === item.to) return;
                          if (!navigationGuard.enter(item.to)) e.preventDefault();
                        }}
                      >
                        <item.icon size={16} aria-hidden />
                        <span>{item.label}</span>
                      </NavLink>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
          <div className="sidebar-foot">
            <button
              type="button"
              className="sidebar-cmdk"
              onClick={() => setCmdkOpen(true)}
              title="快速跳转（Ctrl+K）"
            >
              <Search size={13} aria-hidden />
              快速跳转
              <span className="kbd">Ctrl K</span>
            </button>
          </div>
        </nav>

        {navOpen && (
          <button
            type="button"
            className="sidebar-backdrop"
            aria-label="关闭导航"
            onClick={() => setNavOpen(false)}
          />
        )}

        <div className="content">
          <TopBar onOpenNav={() => setNavOpen(true)} onOpenSearch={() => setCmdkOpen(true)} />
          {/* 滚动容器在内层：顶部条常驻不随内容滚走 */}
          <main className="content-scroll">
            <Suspense fallback={<PageFallback />}>
              <Routes>
                <Route path="/" element={<Navigate to="/traces" replace />} />
                <Route path="/traces" element={<TraceListPage />} />
                <Route path="/traces/:traceId" element={<TraceDetailPage />} />
                <Route path="/spans" element={<SpanListPage />} />
                <Route path="/loops" element={<LoopListPage />} />
                <Route path="/loops/:loopId" element={<LoopDetailPage />} />
                <Route path="/experiments" element={<ExperimentListPage />} />
                <Route path="/datasets" element={<DatasetListPage />} />
                <Route path="/graphs" element={<GraphListPage />} />
                <Route path="/graphs/new" element={<GraphEditorPage />} />
                <Route path="/graphs/:graphId" element={<GraphEditorPage />} />
                <Route path="/playground" element={<PlaygroundPage />} />
                <Route path="/models" element={<ModelConfigPage />} />
                <Route path="/costs" element={<CostPage />} />
                <Route path="/terminal" element={<TerminalPage />} />
                <Route path="/settings" element={<SettingsPage />} />
                <Route path="*" element={<NotFound />} />
              </Routes>
            </Suspense>
          </main>
        </div>
      </div>

      <CommandPalette open={cmdkOpen} onClose={() => setCmdkOpen(false)} />
    </>
  );
}

function NotFound() {
  return (
    <div className="page">
      <div className="empty-state">
        <p>这个地址没有对应页面。</p>
        <NavLink to="/traces" className="btn btn-primary">
          返回 Trace 列表
        </NavLink>
      </div>
    </div>
  );
}
