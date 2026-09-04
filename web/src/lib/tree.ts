/**
 * 树展平与瓶颈标注。纯函数，与 React 无关。
 *
 * 后端已经建好树并算了 self_ms，前端只负责：
 * 1. 按折叠状态展平为可虚拟滚动的扁平列表
 * 2. 标注瓶颈（耗时/成本占比高的节点）
 * 3. 计算关键路径
 */

import type { SpanNode } from "@/api/types";

export interface FlatRow {
  node: SpanNode;
  depth: number;
  /** 是否有子节点（决定是否显示折叠三角） */
  hasChildren: boolean;
  /** 祖先链末端标记，用于画树形连线 */
  isLastChild: boolean;
  /** 在关键路径上 */
  onCriticalPath: boolean;
}

/** 耗时占比超此值标为瓶颈 */
const SLOW_THRESHOLD = 0.3;
/** 成本占比超此值标为高成本 */
const EXPENSIVE_THRESHOLD = 0.4;

export function totalTokens(node: SpanNode): number {
  return (
    node.input_tokens +
    node.output_tokens +
    node.cache_read_tokens +
    node.cache_write_tokens
  );
}

/**
 * 关键路径：从根出发，每层都走 duration_ms 最大的子节点。
 * 这是"优化哪里收益最大"的答案。
 */
export function findCriticalPath(roots: SpanNode[]): Set<string> {
  const path = new Set<string>();
  let current = pickSlowest(roots);

  while (current) {
    path.add(current.span_id);
    current = pickSlowest(current.children);
  }
  return path;
}

function pickSlowest(nodes: SpanNode[]): SpanNode | undefined {
  if (nodes.length === 0) return undefined;
  return nodes.reduce((slowest, node) =>
    node.duration_ms > slowest.duration_ms ? node : slowest,
  );
}

/**
 * 按折叠状态展平。迭代实现 —— 深链路 trace 可达上千层，递归会爆栈。
 */
export function flattenVisible(
  roots: SpanNode[],
  collapsed: ReadonlySet<string>,
  criticalPath: ReadonlySet<string>,
): FlatRow[] {
  const rows: FlatRow[] = [];
  // 栈里存待处理项，逆序压入以保持原顺序输出
  const stack: Array<{ node: SpanNode; depth: number; isLast: boolean }> = [];

  for (let i = roots.length - 1; i >= 0; i -= 1) {
    const node = roots[i];
    if (node) stack.push({ node, depth: 0, isLast: i === roots.length - 1 });
  }

  while (stack.length > 0) {
    const item = stack.pop();
    if (!item) break;
    const { node, depth, isLast } = item;

    rows.push({
      node,
      depth,
      hasChildren: node.children.length > 0,
      isLastChild: isLast,
      onCriticalPath: criticalPath.has(node.span_id),
    });

    if (collapsed.has(node.span_id)) continue;

    for (let i = node.children.length - 1; i >= 0; i -= 1) {
      const child = node.children[i];
      if (child) {
        stack.push({
          node: child,
          depth: depth + 1,
          isLast: i === node.children.length - 1,
        });
      }
    }
  }

  return rows;
}

/**
 * 全树里的失败节点，无视折叠状态。
 *
 * 不能复用 flattenVisible 再筛：它遇到折叠节点就跳过整棵子树，而默认只展开两层，
 * 于是 depth≥2 的错误根本不在候选里 —— 列表页明明显示"失败 3"，
 * 点进来勾"只看失败"却是 0 行，用户会判断错误不在这条 trace 上。
 *
 * 返回扁平列表（depth 归 0）：祖先没被一起显示，保留原缩进会让层级线指向不存在的父行。
 */
export function flattenErrors(
  roots: SpanNode[],
  criticalPath: ReadonlySet<string>,
): FlatRow[] {
  const rows: FlatRow[] = [];
  const stack = [...roots].reverse();

  while (stack.length > 0) {
    const node = stack.pop();
    if (!node) break;

    if (node.status !== "ok") {
      rows.push({
        node,
        depth: 0,
        // 筛选态下三角不可用：这里没有子行可展开，画出来点了没反应
        hasChildren: false,
        isLastChild: true,
        onCriticalPath: criticalPath.has(node.span_id),
      });
    }

    for (let i = node.children.length - 1; i >= 0; i -= 1) {
      const child = node.children[i];
      if (child) stack.push(child);
    }
  }

  return rows;
}

/** 收集所有 span id，用于"全部展开/折叠"。 */
export function collectIds(roots: SpanNode[]): string[] {
  const ids: string[] = [];
  const stack = [...roots];
  while (stack.length > 0) {
    const node = stack.pop();
    if (!node) break;
    ids.push(node.span_id);
    stack.push(...node.children);
  }
  return ids;
}

/** 默认折叠深度：超过此深度的节点初始折叠，避免一屏铺不下。 */
export function collapseBeyondDepth(roots: SpanNode[], maxDepth: number): Set<string> {
  const collapsed = new Set<string>();
  const stack: Array<{ node: SpanNode; depth: number }> = roots.map((node) => ({
    node,
    depth: 0,
  }));

  while (stack.length > 0) {
    const item = stack.pop();
    if (!item) break;
    if (item.depth >= maxDepth && item.node.children.length > 0) {
      collapsed.add(item.node.span_id);
    }
    for (const child of item.node.children) {
      stack.push({ node: child, depth: item.depth + 1 });
    }
  }
  return collapsed;
}

export interface Bottlenecks {
  slow: Set<string>;
  expensive: Set<string>;
}

/**
 * 标注瓶颈。用 self_ms 而非 duration_ms 判断慢节点 ——
 * 父节点 duration 长通常只是在等子节点，标它没有意义。
 */
export function findBottlenecks(roots: SpanNode[], totalMs: number): Bottlenecks {
  const slow = new Set<string>();
  const expensive = new Set<string>();

  const nodes: SpanNode[] = [];
  const stack = [...roots];
  while (stack.length > 0) {
    const node = stack.pop();
    if (!node) break;
    nodes.push(node);
    stack.push(...node.children);
  }

  const totalCost = nodes.reduce(
    (sum, node) => sum + (Number.parseFloat(node.cost_usd) || 0),
    0,
  );

  for (const node of nodes) {
    if (totalMs > 0 && node.self_ms / totalMs >= SLOW_THRESHOLD) {
      slow.add(node.span_id);
    }
    const cost = Number.parseFloat(node.cost_usd) || 0;
    if (totalCost > 0 && cost / totalCost >= EXPENSIVE_THRESHOLD) {
      expensive.add(node.span_id);
    }
  }

  return { slow, expensive };
}
