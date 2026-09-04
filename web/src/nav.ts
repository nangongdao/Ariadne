/**
 * 导航清单 —— 侧边栏与 Ctrl+K 命令面板共用。
 * 单独成模块避免 App ↔ CommandPalette 循环依赖。
 * keywords 供命令面板模糊搜索（中文 + 英文别名）。
 */

import {
  Activity,
  CircleDollarSign,
  Database,
  FlaskConical,
  Layers,
  PlayCircle,
  Repeat,
  Settings as SettingsIcon,
  Sparkles,
  SquareTerminal,
  Workflow,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  /** 命令面板搜索别名（可省略） */
  keywords?: string;
}

export interface NavGroup {
  title: string;
  items: NavItem[];
}

export const NAV_GROUPS: NavGroup[] = [
  {
    title: "观测",
    items: [
      { to: "/traces", label: "Trace", icon: Activity, keywords: "trace 链路 追踪 span" },
      { to: "/spans", label: "Span", icon: Layers, keywords: "span 段 调用" },
    ],
  },
  {
    title: "运行",
    items: [
      { to: "/loops", label: "Loop", icon: Repeat, keywords: "loop 循环 迭代" },
      { to: "/experiments", label: "实验", icon: FlaskConical, keywords: "experiment 评测 eval" },
      { to: "/datasets", label: "数据集", icon: Database, keywords: "dataset 数据" },
    ],
  },
  {
    title: "编排",
    items: [
      { to: "/graphs", label: "编排", icon: Workflow, keywords: "graph 工作流 workflow node" },
      { to: "/playground", label: "Playground", icon: PlayCircle, keywords: "play 调试 对比 compare" },
    ],
  },
  {
    title: "系统",
    items: [
      { to: "/costs", label: "成本", icon: CircleDollarSign, keywords: "cost 费用 token 账单" },
      { to: "/models", label: "模型配置", icon: Sparkles, keywords: "model llm api key apikey baseurl 模型" },
      { to: "/terminal", label: "终端", icon: SquareTerminal, keywords: "terminal shell 终端 命令" },
      { to: "/settings", label: "设置", icon: SettingsIcon, keywords: "settings 配置 key" },
    ],
  },
];
