/**
 * Ariadne TypeScript SDK。
 *
 * 与 Python SDK (ariadne_sdk) 对等能力：
 * - client: Ariadne 客户端 + init/getClient
 * - span: Span 对象 + AsyncLocalStorage 上下文传播
 * - exporter: 后台批量上报（有界队列 + 指数退避重试）
 * - decorators: @trace 装饰器
 * - instrument: OpenAI / Anthropic 自动埋点
 *
 * 最小依赖（仅 Node.js 内置），可独立安装。
 */

export { Ariadne, getClient, init } from "./client";
export { Span, currentSpan, currentTraceId } from "./span";
export type { SpanPayload, UsageData } from "./span";
export { SpanExporter } from "./exporter";
export type { ExporterStats } from "./exporter";
export { trace } from "./decorators";
export { wrapOpenAI } from "./instrument/openai";
export { wrapAnthropic } from "./instrument/anthropic";

export const __version__ = "0.1.0";
