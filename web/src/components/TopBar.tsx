/**
 * 顶部上下文条 —— 常驻显示"我在哪 / 后端是否健康 / 怎么搜"。
 *
 * 环境状态过去只在设置页可见，排查"页面空白"时要先猜是不是后端挂了；
 * 这里做成常驻指示灯并可点进设置页，把诊断路径缩短到一次点击。
 */

import { useQuery } from "@tanstack/react-query";
import { Menu, Search } from "lucide-react";
import { useMemo } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { api } from "@/api/client";
import { ThemeToggle } from "@/components/ThemeToggle";
import { navigationGuard } from "@/lib/navigation-guard";
import { NAV_GROUPS } from "@/nav";

interface TopBarProps {
  onOpenNav: () => void;
  onOpenSearch: () => void;
}

/** 路由 → 面包屑首段。取最长前缀匹配，使详情页也能归到所属板块 */
function useLocationLabel(): string {
  const { pathname } = useLocation();
  return useMemo(() => {
    const items = NAV_GROUPS.flatMap((g) => g.items);
    const hit = items
      .filter((it) => pathname === it.to || pathname.startsWith(`${it.to}/`))
      .sort((a, b) => b.to.length - a.to.length)[0];
    return hit?.label ?? "Ariadne";
  }, [pathname]);
}

type EnvTone = "is-ok" | "is-degraded" | "is-down";

export function TopBar({ onOpenNav, onOpenSearch }: TopBarProps) {
  const label = useLocationLabel();
  const navigate = useNavigate();

  // 30s 一次足够反映连通性；出错时不重试，避免把"后端已挂"拖成 3 次超时
  const { data, isError } = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 30_000,
    retry: false,
  });

  const [tone, text]: [EnvTone, string] = isError
    ? ["is-down", "后端不可达"]
    : data?.status === "ok"
      ? ["is-ok", "服务正常"]
      : data
        ? ["is-degraded", "部分降级"]
        : ["is-degraded", "检测中"];

  const detail = data
    ? `版本 ${data.version}｜ClickHouse ${data.clickhouse ? "在线" : "离线"}｜Redis ${
        data.redis ? "在线" : "离线"
      }`
    : "正在检测后端连通性";

  return (
    <header className="topbar">
      <button type="button" className="btn-icon nav-toggle" onClick={onOpenNav} aria-label="打开导航">
        <Menu size={17} aria-hidden />
      </button>

      <div className="topbar-loc">
        <span>{label}</span>
      </div>

      <div className="topbar-spacer" />

      <button type="button" className="topbar-search" onClick={onOpenSearch}>
        <Search size={14} aria-hidden />
        <span>搜索页面、Trace、Loop</span>
        <span className="kbd">Ctrl K</span>
      </button>

      <ThemeToggle />

      <button
        type="button"
        className={`env-pill ${tone}`}
        onClick={() => {
          if (navigationGuard.enter("/settings")) navigate("/settings");
        }}
        title={`${detail}（点击进入设置）`}
      >
        <span className="env-dot" aria-hidden />
        <span>{text}</span>
      </button>
    </header>
  );
}
