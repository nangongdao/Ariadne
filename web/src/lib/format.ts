/**
 * 展示格式化。
 *
 * 成本用字符串传输（后端 Decimal），这里只在展示时转 number ——
 * 绝不用 number 做成本累加，浮点误差会让账单对不上。
 */

const MS_PER_SEC = 1000;
const MS_PER_MIN = 60 * MS_PER_SEC;

/** 耗时：自动选单位，保持 3 位有效数字左右的可读性。 */
export function formatDuration(ms: number): string {
  if (ms < 1) return "<1ms";
  if (ms < MS_PER_SEC) return `${Math.round(ms)}ms`;
  if (ms < MS_PER_MIN) return `${(ms / MS_PER_SEC).toFixed(2)}s`;
  const minutes = Math.floor(ms / MS_PER_MIN);
  const seconds = ((ms % MS_PER_MIN) / MS_PER_SEC).toFixed(0);
  return `${minutes}m${seconds.padStart(2, "0")}s`;
}

/** Token 数：万级以上缩写，避免表格列宽跳动。 */
export function formatTokens(count: number): string {
  if (count < 1000) return String(count);
  if (count < 1_000_000) return `${(count / 1000).toFixed(1)}k`;
  return `${(count / 1_000_000).toFixed(2)}M`;
}

/**
 * 成本：小额需要更多小数位才有意义。
 * $0.0001 显示成 $0.00 会让用户以为免费。
 */
export function formatCost(usd: string | number): string {
  const value = typeof usd === "string" ? Number.parseFloat(usd) : usd;
  if (!Number.isFinite(value) || value === 0) return "$0";
  if (value < 0.01) return `$${value.toFixed(6).replace(/0+$/, "")}`;
  if (value < 1) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(2)}`;
}

export function formatPercent(ratio: number): string {
  return `${(ratio * 100).toFixed(1)}%`;
}

/** 相对时间。用于列表页，绝对时间放 title 属性里。 */
export function formatRelativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;

  const diffSec = Math.floor((Date.now() - then) / MS_PER_SEC);
  if (diffSec < 5) return "刚刚";
  if (diffSec < 60) return `${diffSec} 秒前`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`;
  if (diffSec < 604800) return `${Math.floor(diffSec / 86400)} 天前`;
  return new Date(iso).toLocaleDateString("zh-CN");
}

export function formatAbsoluteTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    fractionalSecondDigits: 3,
  });
}

/** ID 缩写。前 8 位足够在一次会话内区分，完整值放 title。 */
export function shortId(id: string, length = 8): string {
  return id.length <= length ? id : `${id.slice(0, length)}…`;
}

const KIND_LABELS: Record<string, string> = {
  llm: "LLM",
  tool: "工具",
  rag: "检索",
  code: "代码",
  loop: "Loop",
  harness: "约束",
  eval: "评测",
  internal: "内部",
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

/**
 * 压缩内联的 payload 带 zstd: 前缀（后端分级策略）。
 * 前端无法解压，提示用户这是压缩内容而非乱码。
 */
export function isCompressedPreview(preview: string): boolean {
  return preview.startsWith("zstd:");
}

export function previewText(preview: string, ref: string): string {
  if (isCompressedPreview(preview)) return "（内容已压缩存储，需通过 API 回读）";
  if (!preview && ref) return "（内容已外溢到对象存储）";
  return preview || "（空）";
}
